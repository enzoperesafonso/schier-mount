import logging
import time
import serial
import crc


# --- Custom Exceptions for Clarity ---
class MountError(Exception):
    """Base class for exceptions in this module."""
    pass


class MountConnectionError(MountError):
    """Raised for serial port timeouts, CRC failures, or garbage data."""
    pass


class MountSafetyError(MountError):
    """Raised for safety-related errors like limit switches, E-Stop, or motor faults."""
    pass


class MountMotionError(MountError):
    """Raised when the mount stops but did not reach its target."""
    pass


class MountInputError(MountError):
    """Raised for invalid input values."""
    pass


class MountComm:
    """
    Manages communication with the Schier mount controller.

    This class handles the low-level serial communication protocol, including
    command construction, CRC validation, and response parsing. It provides
    higher-level methods for controlling the mount's motion, homing, and
    status retrieval.

    Args:
        port (str): The serial port to connect to (e.g., "/dev/ttyS0").
        baudrate (int): The baud rate for the serial communication.
    """

    def __init__(self, port: str = "/dev/ttyS0", baudrate=9600, ):
        """Initializes the MountComm object and opens the serial port."""
        self.logger = logging.getLogger("SchierMount")
        self.serial = serial.Serial(port, baudrate, timeout=1.0)
        self.MAX_RETRIES = 3

        self.SLEW_SPEED_RA = 15000
        self.SLEW_SPEED_DEC = 15000

        self.HOME_SPEED_RA = 24382 * 0
        self.HOME_SPEED_DEC = 19395 * 2.0  # seems to be very important!

        self.BIT_MASKS = {
            'ESTOP': 0x0001,
            'NEG_LIM': 0x0002,
            'POS_LIM': 0x0004,
            'BRAKE_ON': 0x0008,
            'AMP_DISABLE': 0x0010
        }

        self.SIDEREAL_RATE = 1


        # ensure the mount doesn't drift while it configures acceleration limits.
        self._send_command("VelRa", 0)
        self._send_command("VelDec", 0)

        # setup acceleration and max velocity using the defaults from original rotsed

        self._send_command("AccelRa", 24382 * 25)
        self._send_command("AccelDec", 19395 * 25)

        self._send_command("MaxVelRA", 24382 * 35)
        self._send_command("MaxVelDec", 19395 * 35)

        # and give the mount a kick ...
        self.recover_servo_state()

    def disconnect(self):
        """
        Safely disconnects from the mount.

        This method stops any ongoing motion and closes the serial port.
        """
        self.logger.debug("Disconnecting Mount!")

        try:

            self.stop_motion()

            time.sleep(0.5)

            self.serial.close()

        except Exception as e:
            self.logger.error(f"Disconnection failed: {e}")

    def recover_servo_state(self):
        """
        Attempts to recover the servo motors from a fault state.

        This sends a sequence of commands to clear any existing faults and
        re-enable the motor amplifiers.
        """
        self.logger.warning("Attempting Servo Recovery (i.e. giving the mount a kick!)...")

        try:

            # The "Kick" !
            # The C code sends Halt, then Stop.
            # Halt kills the trajectory generator. Stop enables the Amps.

            self._send_command("VelRa", 0)
            self._send_command("VelDec", 0)

            self._send_command("HaltRA")
            self._send_command("HaltDec")
            time.sleep(0.2)

            self._send_command("StopRA")
            self._send_command("StopDec")
            time.sleep(0.5)

        except Exception as e:
            self.logger.error(f"Recovery failed: {e}")

    def _stop_axis(self, axis_index: int):
        """
        Sends a Stop command to the specified axis and verifies mount health.

        Args:
            axis_index: The axis to stop (0 for RA, 1 for Dec).

        Raises:
            ValueError: If an invalid axis index is provided.
            MountSafetyError: If the mount reports E-Stop, Brake, or Amp Disable
                              flags after the stop command.
        """

        if axis_index == 0:
            cmd_key = "StopRA"
        elif axis_index == 1:
            cmd_key = "StopDec"
        else:
            raise ValueError("Invalid Axis Index")

        self.logger.debug(f"Stopping Axis {axis_index}...")

        # Send the Stop Command
        # The mount should reply (e.g. "$StopRA<CRC>") confirming receipt.
        try:
            self._send_command(cmd_key)
            ''
            self.logger.debug(f"Axis {axis_index} Stopped Successfully.")

        except MountConnectionError as e:
            # If the stop command fails to send, we are in trouble.
            raise MountSafetyError(f"FAILED TO SEND STOP COMMAND TO AXIS {axis_index}: {e}")

    def _clear_comm(self):
        """
        Clears serial communication buffers and resets the mount's command parser.

        This is a recovery mechanism to be used when communication becomes
        desynchronized. It flushes the serial buffers and sends a carriage
        return to reset the mount's parser.
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
        Validates the integrity of a response from the mount.

        This checks for a valid CRC checksum and verifies that the response
        corresponds to the command that was sent (e.g., an 'RA' command
        receives an 'RA' response).

        Args:
            sent_command: The command string sent to the mount.
            response: The response string received from the mount.

        Returns:
            True if the response is valid, False otherwise.
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
                f"Echo Error: Sent RA command '{sent_command}' but got '{body}'")
            return False

        # Check Dec Axis
        if "Dec" in sent_command and "Dec" not in body:
            self.logger.error(f"Echo Error: Sent Dec command '{sent_command}' but got '{body}'")
            return False

        return True

    def _send_command(self, cmd_key: str, value=None) -> str:
        """
        Sends a command to the mount and waits for a valid response.

        This method constructs a command packet, including the CRC checksum, 
        and sends it to the mount. It will retry the command up to MAX_RETRIES
        times if the communication fails.

        Args:
            cmd_key: The command key (e.g., "VelRa").
            value: An optional value to send with the command.

        Returns:
            The response string from the mount.

        Raises:
            MountConnectionError: If the command fails after all retries.
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
        Retrieves the command and actual encoder positions for a given axis.

        Args:
            axis_index: The axis to query (0 for RA, 1 for Dec).

        Returns:
            A tuple containing the command position and actual position, in
            encoder counts.

        Raises:
            ValueError: If an invalid axis index is provided.
            MountConnectionError: If the position cannot be parsed from the
                                  mount's response.
        """
        if axis_index == 0:
            cmd_key = "Status1RA"
        elif axis_index == 1:
            cmd_key = "Status1Dec"
        else:
            raise ValueError(f"Invalid axis index: {axis_index}")

        # Send Command
        response = self._send_command(cmd_key)

        try:
            # 1. Strip the CRC (Last 4 chars)
            # Raw: "@Status1Dec 1836177.0, 1836177.0 7ED3"
            # Cut: "@Status1Dec 1836177.0, 1836177.0 "
            body = response[:-4].strip()

            # 2. Split using Regex
            # This handles the specific format: Space-Number-Comma-Space-Number
            import re
            tokens = re.split(r'[ ,]+', body)

            # Result tokens: ['@Status1Dec', '1836177.0', '1836177.0']

            if len(tokens) < 3:
                self.logger.error(f"Tokenization failed. Tokens: {tokens}")
                raise ValueError(f"Malformed response: {response}")

            # 3. Parse Numbers
            # We MUST use float() first because the log showed '.0' in the string.
            # int('100.0') crashes Python, but int(float('100.0')) works.
            target_pos = int(float(tokens[1]))
            actual_pos = int(float(tokens[2]))

            return target_pos, actual_pos

        except (ValueError, IndexError) as e:
            self.logger.error(
                f"Parsing Error on {cmd_key}: {e} | Raw: {response}")  # Rykoff got to say "Shite" in his error logging, please can I?
            raise MountConnectionError(f"Failed to parse position: {e}")

    def get_axis_status_bits(self, axis_index: int) -> dict:
        """
        Retrieves and parses the hardware status bits for a given axis.

        Args:
            axis_index: The axis to query (0 for RA, 1 for Dec).

        Returns:
            A dictionary of status flags, including:
            - 'estop': True if the emergency stop is active.
            - 'neg_limit': True if the negative limit switch is active.
            - 'pos_limit': True if the positive limit switch is active.
            - 'brake_on': True if the brake is engaged.
            - 'amp_disabled': True if the motor amplifier is disabled.

        Raises:
            ValueError: If an invalid axis index is provided.
            MountConnectionError: If the status cannot be parsed from the
                                  mount's response.
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
        response = self._send_command(cmd_key)

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

    def get_last_fault(self) -> str:
        """
        Retrieves the last recorded fault string from the mount.

        This command has special behavior: it reads until a semicolon is
        received and does not validate the CRC of the response.

        Returns:
            The fault string from the mount.
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

    def send_home(self):
        """
        Initiates the mount's homing sequence.

        This sets the homing velocity, stops any existing motion, and then
        sends the 'HomeRA' and 'HomeDec' commands to the mount.
        """
        self.logger.info("Initiating Mount Homing Sequence...")

        try:
            # 2. Set Homing Velocities
            # The mount needs to know how fast to spin while looking for the sensor.
            # We set this BEFORE the Home command.
            self.stop_motion()

            self._send_command("VelRa", self.HOME_SPEED_RA)
            self._send_command("VelDec", self.HOME_SPEED_DEC)

            # 3. Stop Motion
            # The C code explicitly calls stop_axis before homing.
            # We use try/except because we want to proceed even if the mount
            # complains that the brake is already on.
            try:
                self.stop_motion()
                # Give it a moment to settle
                time.sleep(0.5)

            except MountSafetyError:
                self.logger.warning("Stop before home reported safety flags (ignoring)")

            # 4. Trigger Homing
            # These commands put the controller into "Homing Mode".
            # It will move until it hits the index mark, then stop and reset its internal counter to 0.
           # self._send_command("HomeRA")
            self._send_command("HomeDec")

            self.logger.debug("Homing Triggered!")

        except MountConnectionError as e:
            self.logger.critical(f"Homing handshake failed: {e}")
            # If comms fail here, we try to stop just in case
            try:
                self.stop_motion()
            except:
                pass
            raise e

    def move_to(self, ra_pos: int, dec_pos: int, speed_ra: float, speed_dec: float):
        """
        Commands the mount to move to a specific encoder position.

        This method sends the target position and velocity to the mount.
        The sequence of commands is important: first the target positions are
        set, then the velocities are set to initiate the motion.

        Args:
            ra_pos: The target RA position in encoder counts.
            dec_pos: The target Dec position in encoder counts.
            speed_ra: The speed for the RA axis in counts/sec.
            speed_dec: The speed for the Dec axis in counts/sec.
        """

        vel_ra = int(speed_ra if speed_ra is not None else self.HOME_SPEED_RA )
        vel_dec = int(speed_dec if speed_dec is not None else self.HOME_SPEED_DEC)

        self.logger.debug(f"Slewing to ({ra_pos}, {dec_pos}) at vel ({vel_ra}, {vel_dec})")

        try:

            # Make sure the mount is actually stopped before any movement or else we get an error!
            self.stop_motion()

            # --- We need to 'ready' the servos for movement before giving commands"---
            self._send_command("RunRa", vel_ra)
            self._send_command("RunDec", vel_dec)

            # --- 3. Send Targets (Load the Registers) ---
            # Note: This does NOT move the mount yet. It just tells the controller
            # "If I tell you to go, this is where you go."
            self._send_command("PosRA", ra_pos)
            self._send_command("PosDec", dec_pos)

            # --- 4. Send Velocities (The Trigger) ---
            # Setting velocity > 0 causes the PID controller to activate and
            # drive towards the 'Pos' target set above.
            self._send_command("VelRa", vel_ra)
            self._send_command("VelDec", vel_dec)


        except MountConnectionError as e:
            # If the command sequence breaks halfway, we are in an unknown state.
            # Best practice: Try to stop immediately.
            self.logger.critical("Slew command sequence failed! Attempting Stop.")
            try:
                self.stop_motion()
            except:
                pass  # We tried our best
            raise e

    # NEVER HALT THE MOUNT, THE BREAKS ARE GONE IT WILL JUST FALL OVER!
    def stop_motion(self):
        """
        Stops all motion on both axes.

        This sends a stop command to each axis and then sets the commanded
        velocity to zero as a safety measure.
        """
        self.logger.debug("Stopping mount!")

        # 1. Send Stop Commands (Highest Priority)
        # We try both even if the first fails
        try:
            self._stop_axis(0)  # RA
        except Exception as e:
            self.logger.error(f"Failed to stop RA: {e}")

        try:
            self._stop_axis(1)  # Dec
        except Exception as e:
            self.logger.error(f"Failed to stop Dec: {e}")

        # Zero the Velocity Registers
        # This ensures that if the 'Stop' latch is released,
        # the mount doesn't try to resume the previous speed.
        try:
            self._send_command("VelRa", 0)
            self._send_command("VelDec", 0)
        except Exception:
            pass  # If comms are bad, we did our best


        # 3. Wait for Settle
        # Important so that subsequent commands don't crash the controller
        time.sleep(0.5)


    def track_sidereal(self):
        pass

