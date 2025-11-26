import logging
from typing import Any, Coroutine
import serial
import time
import asyncio
import crc


# --- Custom Exceptions for Clarity ---
class MountError(Exception):
    """Base class for mount errors."""
    pass


class MountConnectionError(MountError):
    """Serial port timeouts, CRC failures, garbage data."""
    pass


class MountSafetyError(MountError):
    """Limit switches, E-Stop, Motor faults."""
    pass


class MountMotionError(MountError):
    """Mount stopped but did not reach target."""
    pass


class MountComm:
    def __init__(self, port: str = "/dev/ttyS0", baudrate=9600):
        self.logger = logging.getLogger("SchierMount")
        self.serial = serial.Serial(port, baudrate, timeout=1.0)
        self.MAX_RETRIES = 3

        self.BIT_MASKS = {
            'ESTOP': 0x0001,
            'NEG_LIM': 0x0002,
            'POS_LIM': 0x0004,
            'BRAKE_ON': 0x0008,
            'AMP_DISABLE': 0x0010
        }

    def _stop_axis(self, axis_index: int) -> bool:

        # first lets send the stop command ...
        response = ""

        if ra:
            response = self._send_command()
        else:
            response = self._send_command()

        # check e-brake, servo amp, e-stop bit and if actually stopped ...

        self.logger.info(f"Successfully stopped axis!")
        return True

    def _clear_comm(self):
        """
        Clears the serial communication buffers and resets the mount's
        command parser.
        """
        self.logger.debug("Clearing serial comm buffer ...")

        try:
            # 1. Dump any garbage currently in the input buffer
            self.serial.reset_input_buffer()

            # 2. Send a Carriage Return to reset the mount computer's command parser
            self.serial.write(b'\r')

            # 3. Give the hardware a moment to process the CR
            time.sleep(0.1)

            # 4. Read and discard whatever the mount sent back (usually a prompt or error)
            junk = self.serial.read_all()

            if junk:
                self.logger.debug(f"Discarded junk data: {junk}")

            # 5. Ensure the input buffer is purely empty for the next real command
            self.serial.reset_input_buffer()

        except serial.SerialException as e:
            self.logger.error(f"Failed to clear comms: {e}")
            # If we can't even clear the line, the connection is likely dead.
            raise MountConnectionError("Serial port unresponsive during clear.")

    def _validate_response(self, sent_command: str, response: str) -> bool:
        """
        Validates the integrity of the response by:
        1. Checking the CRC checksum.
        2. Verifying the response 'echo' matches the command axis (RA/Dec).

        Args:
            sent_command: The raw string we sent (e.g., "$VelRa, 100")
            response: The raw string received (e.g., "$VelRa, 100a1b2")
                      (Assumes \r has already been stripped)
        """

        # --- 1. Sanity Check ---
        # A valid response must have at least a 1-char body + 4-char CRC
        if not response or len(response) < 5:
            self.logger.error(f"Validation Failed: Response too short ('{response}')")
            return False

        # --- 2. CRC Validation ---
        # The ROTSE protocol puts the 4-character hex CRC at the very end.

        received_crc = response[-4:].lower()  # Extract last 4 chars
        body = response[:-4]  # Extract everything else

        # Calculate what the CRC *should* be based on the body
        calculated_crc = crc.calculate_crc(body).lower()

        if received_crc != calculated_crc:
            self.logger.error(
                f"CRC Mismatch! Body: '{body}' | "
                f"Received: {received_crc} | Calculated: {calculated_crc}"
            )
            return False

        # --- 3. Echo/Context Check ---
        # Ensures we didn't get a 'Dec' response to an 'RA' command.
        # This prevents mix-ups if the serial buffer got out of sync.

        # Check RA Axis
        if "RA" in sent_command and "RA" not in body:
            self.logger.error(
                f"Echo Error: Sent RA command '{sent_command}' but got '{body}'")  # Rykoff got to say "Shite" in his error logging :(
            return False

        # Check Dec Axis
        if "Dec" in sent_command and "Dec" not in body:
            self.logger.error(f"Echo Error: Sent Dec command '{sent_command}' but got '{body}'")
            return False

        return True

    def send_command(self, cmd_key: str, value=None) -> str:
        """
        Constructs a command packet, sends it to the mount, and returns the verified response.

        Args:
            cmd_key: The command mnemonic (e.g., "VelRa", "PosDec")
            value: Optional integer/float value to append (e.g., 1000)

        Returns:
            str: The valid response string from the mount (excluding \r).

        Raises:
            MountConnectionError: If communication fails after MAX_RETRIES.
        """

        # --- 1. Construct the Packet ---
        # Format: "$Key, Value" or "$Key"
        if value is not None:
            raw_cmd = f"${cmd_key}, {value}"
        else:
            raw_cmd = f"${cmd_key}"

        # Calculate CRC (using the external function)
        # Note: We calculate CRC on the body "$Cmd, Val"
        crc_hex = crc.calculate_crc(raw_cmd)

        # Final packet: "$Cmd, Val<CRC>\r"
        # The ROTSE protocol appends CRC directly to the end, then CR.
        final_packet_str = f"{raw_cmd}{crc_hex}\r"
        final_packet_bytes = final_packet_str.encode('ascii')

        # --- 2. The Retry Loop ---
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:

                # Clear input buffer to ensure we don't read old garbage.
                self.serial.reset_input_buffer()

                self.serial.write(final_packet_bytes)

                # Blocks until \r is seen or timeout (1.0s) occurs
                raw_response = self.serial.read_until(b'\r')

                # D. Check Timeout
                if not raw_response:
                    raise MountConnectionError("Timeout: Mount did not respond.")

                # Decode to string (strips whitespace and \r)
                response_str = raw_response.decode('ascii').strip()

                # E. Validate (CRC & Echo)
                # We pass 'raw_cmd' to check that the mount echoed the correct axis
                if self._validate_response(raw_cmd, response_str):
                    # SUCCESS: Return the valid response
                    return response_str
                else:
                    raise MountConnectionError(f"Validation failed on: {response_str}")

            except (UnicodeDecodeError, MountConnectionError, serial.SerialException) as e:
                last_error = e
                self.logger.warning(f"Command '{cmd_key}' attempt {attempt + 1} failed: {e}")

                # F. Recovery Phase
                # If the comms failed (bad CRC, timeout), the line might be dirty.
                # Flush it before the next attempt.
                try:
                    self._clear_comm()
                except Exception:
                    pass  # Ignore errors during recovery, just try loop again

                # Short pause to let hardware settle
                time.sleep(0.2)

        # --- 3. Critical Failure ---
        self.logger.error(f"Critical: Failed to send {cmd_key} after {self.MAX_RETRIES} attempts.")
        raise MountConnectionError(f"Hard failure sending {cmd_key}: {last_error}")

    def get_encoder_position(self, axis_index: int) -> tuple[int, int]:
        """
        Retrieves the Command (Target) and Actual (Encoder) positions.

        Args:
            axis_index: 0 for RA, 1 for Dec.

        Returns:
            tuple: (target_position, actual_position) in encoder counts.
        """

        if axis_index == 0:
            cmd_key = "Status1RA"
        elif axis_index == 1:
            cmd_key = "Status1Dec"
        else:
            raise ValueError(f"Invalid axis index: {axis_index} (Must be 0 or 1)")

        # Send Command and Get Response
        response = self.send_command(cmd_key)

        # Parse the Response
        try:
            # Strip the 4-char CRC from the end first to avoid parsing errors
            clean_response = response[:-4]

            # Split by comma
            parts = clean_response.split(',')

            # Parts should look like: ['$Status1RA', ' 1000', ' 1005', ' 0000']
            if len(parts) < 3:
                raise ValueError(f"Malformed response: {response}")

            # Parse integers (Python handles the leading spaces automatically)
            target_pos = int(parts[1])
            actual_pos = int(parts[2])

            return target_pos, actual_pos

        except (ValueError, IndexError) as e:
            self.logger.error(f"Parsing Error on {cmd_key}: {e} | Raw: {response}")
            raise MountConnectionError(f"Failed to parse position: {e}")

    def _get_axis_status_bits(self, axis_index: int) -> dict:
        """
        Retrieves the hardware status words and parses safety flags.

        Args:
            axis_index: 0 for RA, 1 for Dec.

        Returns:
            dict: A dictionary containing raw words and interpreted flags.
                  e.g. {'estop': False, 'brakes': True, 'raw_w1': 0x1234...}
        """

        if axis_index == 0:
            cmd_key = "Status2RA"
        elif axis_index == 1:
            cmd_key = "Status2Dec"
        else:
            raise ValueError("Invalid Axis")

        # 2. Send & Receive
        # Expected Format: "$Status2RA, <Word1_Hex>, <Word2_Hex><CRC>"
        # Example: "$Status2RA, 0000, 0010A1B2"
        response = self.send_command(cmd_key)

        try:
            clean_response = response[:-4]  # Strip CRC
            parts = clean_response.split(',')

            if len(parts) < 3:
                raise ValueError("Malformed response")

            # Parse Hex Strings to Integers
            word1 = int(parts[1].strip(), 16)
            word2 = int(parts[2].strip(), 16)

            # In the C code:
            # word1 contained: ESTOP, NEG_LIM, POS_LIM
            # word2 contained: BRAKE, AMP_DIS

            status = {
                'raw_word1': word1,
                'raw_word2': word2,

                # Check bits using Bitwise AND (&)
                'estop': bool(word1 & self.BIT_MASKS['ESTOP']),
                'neg_limit': bool(word1 & self.BIT_MASKS['NEG_LIM']),
                'pos_limit': bool(word1 & self.BIT_MASKS['POS_LIM']),
                'brake_on': bool(word2 & self.BIT_MASKS['BRAKE_ON']),
                'amp_disabled': bool(word2 & self.BIT_MASKS['AMP_DISABLE']),
            }

            # Log warnings if critical bits are set
            if status['estop']:
                self.logger.critical(f"Axis {axis_index}: E-STOP ACTIVE!")
            if status['neg_limit'] or status['pos_limit']:
                self.logger.warning(f"Axis {axis_index}: Limit Switch Hit")

            return status

        except (ValueError, IndexError) as e:
            self.logger.error(f"Status2 Parse Error: {e} | Raw: {response}")
            raise MountConnectionError(f"Failed to parse status: {e}")

    def _get_status_3(self):
        pass

    def get_last_fault(self) -> str:
        """
        Retrieves the last recorded fault string from the mount.

        Special Behavior:
        - Terminates read on semicolon ';'.
        - Does NOT validate CRC on response (per original C code).
        - Flushes buffer immediately after reading.

        Equivalent to C: get_last_fault()
        """
        # You need to find the string value for 'RecentFaults' in your C header.
        # It is likely just "RecentFaults" or "GetFaults".
        cmd_key = "RecentFaults"

        # 1. Construct Command (Standard Format)
        raw_cmd = f"${cmd_key}"
        crc_hex = crc.calculate_crc(raw_cmd)
        final_packet = f"{raw_cmd}{crc_hex}\r"

        try:
            self.serial.reset_input_buffer()
            self.serial.write(final_packet.encode('ascii'))

            # 2. Read until Semicolon (Specific to this command)
            # The C code: mount_serial_read(..., ';', ...)
            response = self.serial.read_until(b';')

            if not response:
                raise MountConnectionError("Timeout waiting for fault string")

            # 3. Decode
            # The C code treats this as a raw human-readable string.
            fault_text = response.decode('ascii', errors='ignore').strip()

            # 4. Flush the 'Tail'
            # The C code had a 'while(select...)' loop here to eat remaining chars.
            # We assume the mount sends [Text];[CR][LF] or similar.
            # We grabbed up to ';', so we dump the rest now.
            time.sleep(0.1)
            self.serial.reset_input_buffer()

            # 5. Check for Critical Errors (As seen in C evalstat)
            if "High Output I^2" in fault_text:
                self.logger.critical(f"UNRECOVERABLE HARDWARE FAULT: {fault_text}")
                raise MountSafetyError(f"Hardware Failure Check Mount Computer!: {fault_text}")

            return fault_text

        except serial.SerialException as e:
            self.logger.error(f"Failed to get fault history: {e}")
            return "Error retrieving fault"

    def _stop_move(self):
        pass

    def _zero_encoder_positions(self):
        pass

    def initialise_mount(self):
        pass

    def home_mount(self):
        pass

    def slew_mount(self, ra_enc: float, dec_enc: float, stop_mount=True):

        pass

    def abort_slew_mount(self):

        pass

    def track_sidereal(self):
        pass

    def get_mount_status(self):
        pass
