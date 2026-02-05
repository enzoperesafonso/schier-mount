import logging
import time
import serial
import crc
from configuration import MountConfig


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
        config_file (str): Path to the configuration YAML file.
    """

    def __init__(self, port: str = "/dev/ttyS0", baudrate=9600, config = MountConfig()):
        """Initializes the MountComm object and opens the serial port."""
        self.logger = logging.getLogger("SchierMount")

        # Load Configuration
        self.config = config

        self.serial = serial.Serial(port, baudrate, timeout=1.0)

        self.BIT_MASKS = {
            'ESTOP': 0x0001,
            'NEG_LIM': 0x0002,
            'POS_LIM': 0x0004,
            'BRAKE_ON': 0x0008,
            'AMP_DISABLE': 0x0010
        }

        self.ra_target_enc = 0
        self.dec_target_enc = 0

    def disconnect(self):
        """
        Safely disconnects from the mount.

        This method stops any ongoing motion and closes the serial port.
        """
        self.logger.debug("Disconnecting Mount!")

        try:

            # zero the velocities so the mount does not lurch after stop is released ...
            self._send_command("VelRa", 0)
            self._send_command("VelDec", 0)

            self._send_command("StopRA")
            self._send_command("StopDec")

            # check if we do not have any error status bits after stopping amps, if so we cannot run!
            if self.get_axis_status_bits(0)['any_error'] or self.get_axis_status_bits(1)['any_error']:
                raise MountError()

            time.sleep(0.5)

            self.serial.close()

        except Exception as e:
            self.logger.error(f"Disconnection failed: {e}")

    def init_mount(self):
        """
        Initializes the mount hardware with default motion parameters.

        This method sets the acceleration and maximum velocity limits for both
        axes and performs a Halt/Stop sequence to reset the servo amplifiers
        and velocity curves.
        """
        self.logger.debug("Initiating the Mount!")

        try:


            # reset mounts command parser!
            #self._clear_comm()

            # zero the mount velocities

            self._send_command("VelRa", 0)
            self._send_command("VelDec", 0)

            # setup acceleration and max velocity using the config

            accel_ra = int(self.config.acceleration['slew_ra'] * self.config.encoder['steps_per_deg_ra'])
            accel_dec = int(self.config.acceleration['slew_dec'] * self.config.encoder['steps_per_deg_dec'])

            self._send_command("AccelRa", accel_ra)
            self._send_command("AccelDec", accel_dec)

            max_ra = int(self.config.speeds['max_ra'] * self.config.encoder['steps_per_deg_ra'])
            max_dec = int(self.config.speeds['max_dec'] * self.config.encoder['steps_per_deg_dec'])

            self._send_command("MaxVelRA", max_ra)
            self._send_command("MaxVelDec", max_dec)

            # we need to halt the mount to deactivate the amps, then stop to re-engage them!
            # need this to reset velocity curves but sketchy without physical breaks so beware ...

            self._send_command("HaltRA")
            self._send_command("StopRA")
            time.sleep(0.2)

            self._send_command("HaltDec")
            self._send_command("StopDec")
            time.sleep(0.2)

        except MountConnectionError as e:
            self.logger.error(f"Mount initialization failed: {e}")
            raise

    def home_mount(self):
        """
        Initiates the homing sequence for both axes.

        This method sets the homing velocities from the configuration,
        stops the servos and resets velocity curves, and triggers the hardware homing
        routine for RA and Dec.
        """
        self.logger.debug("Sending the mount home!")

        try:

            # mount has to be in STOP else it will freeze serial!
            self._send_command("StopRA")
            self._send_command("StopDec")

            # check if we do not have any error status bits, if so we cannot home!
            if self.get_axis_status_bits(0)['any_error'] or self.get_axis_status_bits(1)['any_error']:
                raise MountError()

            self._send_command("VelRa", self.config.speeds['home_ra'])
            self._send_command("VelDec", self.config.speeds['home_dec'])

            self._send_command("HomeRA",1)
            self._send_command("HomeDec",1)
        except Exception as e:
            self.logger.error(f"Failed to home run command: {e}")
            raise

    def run_mount(self):
        """
        Activates the motor amplifiers for both axes.

        This method transitions the mount from a stopped or halted state to
        a running state. It sets the velocities to zero and sends the Run command
        to engage the servos, allowing the mount to respond to motion commands.
        """
        self.logger.debug("Sending run command to mount!")

        try:

            # zero the velocities so the mount does not lurch after stop is released ...
            self._send_command("VelRa", 0)
            self._send_command("VelDec", 0)

            self._send_command("StopRA")
            self._send_command("StopDec")

            # check if we do not have any error status bits after stopping amps, if so we cannot run!
            if self.get_axis_status_bits(0)['any_error'] or self.get_axis_status_bits(1)['any_error']:
                raise MountError()

            self._send_command("RunRA")
            self._send_command("RunDec")

        except Exception as e:
            self.logger.error(f"Failed to send run command: {e}")
            raise

    def park_mount(self):
        """
        Moves the mount to its designated park position.

        The park position and the speed used for the move are retrieved from
        the configuration. This method calculates the target encoder counts
        based on the park coordinates and the encoder zero points.
        """
        self.logger.debug("Parking the mount!")

        try:

            ra_speed = self.config.speeds['home_ra'] * self.config.encoder['steps_per_deg_ra']
            dec_speed = self.config.speeds['home_dec'] * self.config.encoder['steps_per_deg_dec']

            park_ra = self.config.park['ra'] * self.config.encoder['steps_per_deg_ra'] + self.config.encoder[
                'zeropt_ra']
            park_dec = self.config.park['dec'] * self.config.encoder['steps_per_deg_dec'] + self.config.encoder[
                'zeropt_dec']

            self.ra_target_enc = park_ra
            self.dec_target_enc = park_dec

            self._move_mount(park_ra, park_dec, ra_speed, dec_speed, stop=True)

        except Exception as e:
            self.logger.error(f"Failed to send park commands: {e}")
            raise

    def standby_mount(self):
        """ #TODO UPdat  se
        Moves the mount to its designated standby position.

        The standby zenith position and the speed used for the move are retrieved from
        the configuration. This method calculates the target encoder counts
        based on the park coordinates and the encoder zero points.
        """
        self.logger.debug("Sending mount the mount to zenith!")

        try:

            ra_speed = self.config.speeds['home_ra'] * self.config.encoder['steps_per_deg_ra']
            dec_speed = self.config.speeds['home_dec'] * self.config.encoder['steps_per_deg_dec']

            park_ra = self.config.standby['ra'] * self.config.encoder['steps_per_deg_ra'] + self.config.encoder[
                'zeropt_ra']
            park_dec = self.config.standby['dec'] * self.config.encoder['steps_per_deg_dec'] + self.config.encoder[
                'zeropt_dec']

            self.ra_target_enc = park_ra
            self.dec_target_enc = park_dec

            self._move_mount(park_ra, park_dec, ra_speed, dec_speed, stop=True)

        except Exception as e:
            self.logger.error(f"Failed to send park commands: {e}")
            raise

    def shift_mount(self, ra_delta_enc: int, dec_delta_enc: int):
        """
        Performs a relative move (shift) from the current target position.

        This method calculates a new target by adding the provided encoder
        deltas to the current commanded positions and initiates a move
        at 'fine' speeds without stopping current motion first.

        Args:
            ra_delta_enc (int): Relative RA movement in encoder counts.
            dec_delta_enc (int): Relative Dec movement in encoder counts.
        """

        try:

            self.logger.debug("Shifting the mount!")

            # calculate the shift velocity in encoder steps per second
            ra_vel = self.config.speeds['fine_ra'] * self.config.encoder['steps_per_deg_ra']
            dec_vel = self.config.speeds['fine_dec'] * self.config.encoder['steps_per_deg_dec']

            # get the final new position in encoder steps
            ra_enc = self.get_encoder_position(0)[0] + ra_delta_enc
            dec_enc = self.get_encoder_position(1)[0] + dec_delta_enc

            self.ra_target_enc = ra_enc
            self.dec_target_enc = dec_enc

            # send the new move command ...
            self._move_mount(ra_enc, dec_enc, ra_vel, dec_vel, stop=False)

        except Exception as e:
            self.logger.error(f"Failed to send shift mount commands: {e}")
            raise e

    def zero_mount(self):
        """
        Updates the configuration zero points using the current encoder positions.

        This method queries the mount for the current RA and Dec encoder counts
        and updates the internal configuration object. This is typically used
        after a manual alignment or homing sequence to establish the reference
        frame.
        """
        self.logger.debug("Zeroing the mount to current position...")

        try:
            # Get current actual encoder positions (index 1 of the returned tuple)
            ra_pos = self.get_encoder_position(0)[1]
            dec_pos = self.get_encoder_position(1)[1]

            # Update the configuration zero points
            self.config.encoder['zeropt_ra'] = ra_pos
            self.config.encoder['zeropt_dec'] = dec_pos

            self.logger.info(f"Mount zeroed. New Zero Points - RA: {ra_pos}, Dec: {dec_pos}")

        except Exception as e:
            self.logger.error(f"Failed to zero mount: {e}")
            raise

    def slew_mount(self, ra_enc: int, dec_enc: int):
        """
        Initiates a slew to the specified RA and Dec encoder positions.

        The slew is performed at the slew speeds defined in the configuration.

        Args:
            ra_enc (int): Target RA position in encoder counts.
            dec_enc (int): Target Dec position in encoder counts.
        """
        self.logger.debug(f"Slewing mount to RA: {ra_enc}, Dec: {dec_enc}")

        try:
            ra_vel = self.config.speeds['slew_ra'] * self.config.encoder['steps_per_deg_ra']
            dec_vel = self.config.speeds['slew_dec'] * self.config.encoder['steps_per_deg_dec']

            self.ra_target_enc = ra_enc
            self.dec_target_enc = dec_enc

            self._move_mount(ra_enc, dec_enc, ra_vel, dec_vel, stop=False)
        except Exception as e:
            self.logger.error(f"Failed to initiate slew: {e}")
            raise

    def track_mount(self, ra_vel: int, dec_vel: int):
        """
        Sets the mount to track at specific velocities.

        This method calculates the target positions based on the direction of
        the provided velocities (moving toward the software limits) and
        initiates the move.

        Args:
            ra_vel (int): RA velocity in encoder counts per second.
            dec_vel (int): Dec velocity in encoder counts per second.
        """
        try:
            self.logger.debug(f"Tracking mount at VelRA: {ra_vel}, VelDec: {dec_vel}")

            # Determine target based on velocity direction
            ra_limit = 'ra_max' if ra_vel >= 0 else 'ra_min'
            dec_limit = 'dec_max' if dec_vel >= 0 else 'dec_min'

            ra_target = self.config.limits[ra_limit] * self.config.encoder['steps_per_deg_ra'] + self.config.encoder['zeropt_ra']
            dec_target = self.config.limits[dec_limit] * self.config.encoder['steps_per_deg_dec'] + self.config.encoder['zeropt_dec']

            self.ra_target_enc = ra_target
            self.dec_target_enc = dec_target

            self._move_mount(ra_target, dec_target, abs(ra_vel), abs(dec_vel), stop=False)

        except Exception as e:
            self.logger.error(f"Failed to initiate tracking: {e}")
            raise

    def idle_mount(self):
        """
         Places the mount in an idle state.

         This method is intended to stop any active tracking or slewing
         by setting velocities to zero, while maintaining the servo
         loop and amplifier state.
         """
        try:

            self.logger.debug("Idling the Mount!")

            # Zero the Velocity Registers

            self._send_command("VelRa", 0)
            self._send_command("VelDec", 0)

            time.sleep(1.0)

            ra_now = self.get_encoder_position(0)[1]
            dec_now = self.get_encoder_position(1)[1]

            self.ra_target_enc = ra_now
            self.dec_target_enc = dec_now

            self._send_command("PosRA", ra_now)
            self._send_command("PosDec", dec_now)

        except Exception as e:

            self.logger.error(f"Failed when trying to idle mount: {e}")

            raise

    def _move_mount(self, ra_enc, dec_enc, ra_vel, dec_vel, stop=True):
        """
        Internal method to move the mount to specific encoder positions.

        This method performs safety limit checks against the configuration
        before sending the position and velocity commands to the controller.

        Args:
            ra_enc: Target RA position in encoder counts.
            dec_enc: Target Dec position in encoder counts.
            ra_vel: RA velocity in encoder counts per second.
            dec_vel: Dec velocity in encoder counts per second.
            stop: Whether to stop current motion before initiating the move.

        Raises:
            MountSafetyError: If the target positions are outside the software limits.
            MountConnectionError: If communication with the mount fails.
        """
        self.logger.debug("Sending a move to the Mount!")

        try:

            # stop the mount before moving if requested
            if stop:
                self._send_command("VelRa", 0)
                self._send_command("VelDec", 0)

            # check if ra is (as Rykoff puts it) kosher ...
            if (ra_enc > (self.config.limits['ra_max'] * self.config.encoder['steps_per_deg_ra'] + self.config.encoder[
                'zeropt_ra']) or ra_enc < self.config.limits['ra_min'] * self.config.encoder['steps_per_deg_ra'] +
                    self.config.encoder['zeropt_ra']):
                raise MountSafetyError()

            # now if dec is too ...
            if (dec_enc > (
                    self.config.limits['dec_max'] * self.config.encoder['steps_per_deg_dec'] + self.config.encoder[
                'zeropt_dec']) or dec_enc < self.config.limits['dec_min'] * self.config.encoder['steps_per_deg_dec'] +
                    self.config.encoder['zeropt_dec']):

                print(f'Dec out of bound {  self.config.limits['dec_max'] * self.config.encoder['steps_per_deg_dec'] + self.config.encoder[
                'zeropt_dec']} and {self.config.limits['dec_min'] * self.config.encoder['steps_per_deg_dec'] +
                    self.config.encoder['zeropt_dec']}')
                raise MountSafetyError()

            # set the positions ...
            self._send_command("PosRA", ra_enc)
            self._send_command("PosDec", dec_enc)

            # set the velocities and away we go ...
            self._send_command("VelRa", ra_vel)
            self._send_command("VelDec", dec_vel)


        except Exception as e:
            # Ensure we don't leave the mount in a weird state if a command fails
            self.logger.error(f"Failed when trying to move mount: {e}")
            raise e

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

    def _send_command(self, cmd_key: str, value=None, retries = 3) -> str:
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

        for attempt in range(retries):
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
        self.logger.error(f"Critical: Failed to send {cmd_key} after {retries} attempts.")
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
            target = self.ra_target_enc
        elif axis_index == 1:
            cmd_key = "Status1Dec"
            target = self.dec_target_enc
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
            target_pos = target
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
            - 'any_error': True if ANY of the above flags are active.

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
            raise ValueError(f"Invalid Axis Index: {axis_index}")

        # 1. Send & Receive
        # Expected Format: "$Status2RA, <Word1_Hex>, <Word2_Hex><CRC>"
        response = self._send_command(cmd_key)

        try:
            clean_response = response[:-4]  # Strip CRC
            parts = clean_response.split(',')

            if len(parts) < 3:
                raise ValueError(f"Malformed response: {response}")

            # 2. Parse Hex Strings to Integers
            word1 = int(parts[1].strip(), 16)
            word2 = int(parts[2].strip(), 16)

            # 3. Check bits using Bitwise AND (&)
            # word1 contains: ESTOP, NEG_LIM, POS_LIM
            # word2 contains: BRAKE, AMP_DIS
            estop_active = bool(word1 & self.BIT_MASKS['ESTOP'])
            neg_limit_active = bool(word1 & self.BIT_MASKS['NEG_LIM'])
            pos_limit_active = bool(word1 & self.BIT_MASKS['POS_LIM'])
            brake_active = bool(word2 & self.BIT_MASKS['BRAKE_ON'])
            amp_disabled = bool(word2 & self.BIT_MASKS['AMP_DISABLE'])

            # 4. Build the Status Dictionary
            status = {'raw_word1': word1, 'raw_word2': word2, 'estop': estop_active, 'neg_limit': neg_limit_active,
                      'pos_limit': pos_limit_active, 'brake_on': brake_active, 'amp_disabled': amp_disabled,
                      'any_error': (
                              estop_active or
                              neg_limit_active or
                              pos_limit_active or
                              brake_active or
                              amp_disabled
                      )}

            return status

        except ValueError as e:
            self.logger.error(f"Failed to parse status response: {e}")
            raise MountConnectionError(f"Status Parse Error: {e}")

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
