import asyncio
import logging
import math
from enum import Enum, auto

from comm import MountComm
from configuration import MountConfig
from coordinates import MountCoordinates


class MountState(Enum):
    IDLE = auto()
    SLEWING = auto()
    TRACKING = auto()
    PARKING = auto()
    PARKED = auto()
    HOMING = auto()
    FAULT = auto()
    RECOVERING = auto()
    UNKNOWN = auto()

class SchierMount():

    def __init__(self):

        self.logger = logging.getLogger("SchierMount")

        self._status_task = None
        self._move_task = None  # Track the active move

        self.serial_lock = asyncio.Lock()

        self.current_positions = {
            "ra_enc": 0, "ra_target_enc": 0,
            "dec_enc": 0, "dec_target_enc": 0,
        }

        self.ra_offset_deg = 0.0
        self.dec_offset_deg = 0.0

        self.config = MountConfig()
        self.coord = MountCoordinates(config=MountConfig())
        self.comm = MountComm(config=self.config)

        self.state = MountState.UNKNOWN

    async def init_mount(self):
        """
        Initializes the mount hardware and starts the background status monitoring loop.

        This method sends the initialization command to the hardware, sets the initial
        state to PARKED, and ensures the status polling task is running.

        Raises:
            Exception: If hardware initialization fails.
        """
        try:
            self.logger.info("Initializing mount hardware...")
            await self._safe_comm(self.comm.init_mount)
            self.state = MountState.PARKED
            if self._status_task is None or self._status_task.done():
                self._status_task = asyncio.create_task(self._status_loop())
            self.logger.info("Mount initialization complete.")
        except Exception as e:
            self.state = MountState.UNKNOWN
            self.logger.error(f"Failed to initialize mount: {e}")
            raise

    async def home_mount(self):
        """
        Initiates the homing sequence for both axes.

        This method performs the following steps:
        1. Sets the mount state to HOMING.
        2. Sends the hardware homing command to the controller.
        3. Monitors encoder feedback until movement stops (within tolerance).
        4. Resets the internal encoder counts to zero at the home position.
        5. Transitions the mount state to IDLE.

        Raises:
            TimeoutError: If the mount fails to stabilize at home within the timeout.
            Exception: For communication or hardware errors during the sequence.
        """
        try:
            self.logger.debug("Starting homing sequence...")
            self.state = MountState.HOMING
            self._move_task = asyncio.current_task()

            # Use safe_comm to send the homing command
            await self._safe_comm(self.comm.home_mount)

            self.logger.debug("Homing command sent, waiting for encoders to stabilize...")
            await self._await_encoder_stop(tolerance=100, timeout=120)

            await self._safe_comm(self.comm.zero_mount)

            self.state = MountState.IDLE
            self.logger.info("Homing sequence completed successfully.")
        except Exception as e:
            logging.error(f"Failed to home mount: {e}")
        finally:
            self._move_task = None

    async def stop_mount(self):
        """
        Immediately stops all mount movement and cancels active movement tasks.

        This method:
        1. Sends an idle command to the hardware to stop motor movement.
        2. Sets the mount state to IDLE.
        3. Cancels any running asynchronous movement tasks (e.g., homing or parking).
        """
        self.logger.info("Stopping mount...")

        # 1. Stop the Hardware
        await self._safe_comm(self.comm.idle_mount)
        self.state = MountState.IDLE

        # 2. Stop the Software Task
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()

    async def park_mount(self):
        """
        Initiates the parking sequence for the mount.

        This method performs the following steps:
        1. Sets the mount state to PARKING.
        2. Sends the hardware homing command to move the mount to its park position.
        3. Monitors encoder feedback until movement reaches target (within tolerance).
        4. Transitions the mount state to PARKED.

        Raises:
            TimeoutError: If the mount fails to stabilize at the park position within the timeout.
            Exception: For communication or hardware errors during the sequence.
        """
        try:
            self.logger.info("Parking mount...")
            self.state = MountState.PARKING
            self._move_task = asyncio.current_task()

            # Use safe_comm to send the park command
            await self._safe_comm(self.comm.park_mount)

            self.logger.debug("Parking command sent, waiting for encoders to reach target...")
            await self._await_encoder_stop(tolerance=100, timeout=120)

            self.state = MountState.PARKED
            self.logger.info("Homing sequence completed successfully.")

        except Exception as e:
            logging.error(f"Failed to park mount: {e}")
        finally:
            self._move_task = None

    async def standby_mount(self):
        """
        Moves the mount to the standby (zenith) position.

        This method:
        1. Sets the mount state to SLEWING.
        2. Sends the hardware command to move to the standby position.
        3. Monitors encoder feedback until movement stops (within tolerance).
        4. Transitions the mount state to IDLE.

        Raises:
            TimeoutError: If the mount fails to stabilize at the standby position within the timeout.
            Exception: For communication or hardware errors during the sequence.
        """
        try:
            self.logger.info("Sending mount to standby position (zenith) ...")
            self.state = MountState.SLEWING
            self._move_task = asyncio.current_task()

            # Use safe_comm to send the park command
            await self._safe_comm(self.comm.standby_mount)

            self.logger.debug("Standby command sent, waiting for encoders to reach target...")
            await self._await_encoder_stop(tolerance=100, timeout=120)

            self.state = MountState.IDLE
            self.logger.info("Mount moved to standby pos.")

        except Exception as e:
            logging.error(f"Failed to move mount: {e}")
        finally:
            self._move_task = None

    async def slew_mount(self, ra_deg : float, dec_deg : float ):
        """
        Slews the mount to the specified RA and Dec coordinates.

        Args:
            ra_deg (float): Target Right Ascension in degrees.
            dec_deg (float): Target Declination in degrees.

        Steps:
            1. Applies software offsets to the target coordinates.
            2. Converts the target RA/Dec to encoder steps.
            3. Commands the hardware to slew to the target encoder positions.
            4. Monitors the movement until the target is reached.

        Raises:
            TimeoutError: If the mount fails to reach the target within the timeout.
            Exception: For communication or hardware errors.
        """
        try:
            self.logger.info(f"Slewing to RA: {ra_deg}, Dec: {dec_deg}...")
            self.state = MountState.SLEWING
            self._move_task = asyncio.current_task()

            # 1. Apply software offsets
            target_ra = ra_deg + self.ra_offset_deg
            target_dec = dec_deg + self.dec_offset_deg

            # 2. Convert to encoder steps
            ra_steps, dec_steps = self.coord.radec_to_enc(target_ra, target_dec)

            # 3. Send hardware command
            await self._safe_comm(self.comm.slew_mount, int(ra_steps), int(dec_steps))

            # 4. Wait for completion
            await self._await_mount_at_position()

            self.state = MountState.IDLE
            self.logger.info("Slew completed successfully.")

        except Exception as e:
            self.logger.error(f"Slew failed: {e}")
            self.state = MountState.FAULT
            raise
        finally:
            self._move_task = None

    async def track_sidereal(self):
        """
        Starts sidereal tracking on the RA axis.

        Calculates the sidereal rate in steps per second based on the mount's
        configuration. Note that for the Southern Hemisphere, the RA motor
        direction is inverted.

        Transitions the mount state to TRACKING.

        Raises:
            Exception: If the tracking command fails to send to the hardware.
        """
        try:
            self.logger.info("Starting sidereal tracking...")
            self.state = MountState.TRACKING

            # since we are in the SOUTHERN HEMISPHERE we need to flip the ra motor direction ...
            sidereal_rate_steps_per_sec = -1 * 0.004178 * self.config.encoder['steps_per_deg_ra']

            await self._safe_comm(self.comm.track_mount, sidereal_rate_steps_per_sec, 0.0)

            self.logger.info("Mount is now tracking at sidereal rate.")
        except Exception as e:
            self.state = MountState.FAULT
            self.logger.error(f"Failed to start sidereal tracking: {e}")
            raise

    async def shift_mount(self, delta_ra: float, delta_dec: float):
        """
        Shifts the mount by a relative amount of degrees in RA and Dec.
        Uses cosine projection to ensure 'delta_ra' represents true angular
        distance on the sky regardless of proximity to the poles.

        Args:
            delta_ra (float): The relative shift in Right Ascension (degrees).
            delta_dec (float): The relative shift in Declination (degrees).

        Steps:
            1. Retrieves current RA/Dec.
            2. Applies cosine correction (secant of Dec) to RA to maintain true angular distance.
            3. Converts corrected degrees to encoder steps.
            4. Commands the hardware to perform a relative move.
            5. Waits for the mount to reach the target position.

        Raises:
            TimeoutError: If the mount fails to reach the target within the timeout.
            Exception: For communication or hardware errors.
        """
        try:
            self.state = MountState.SLEWING
            self._move_task = asyncio.current_task()

            # 1. Get current position (Assuming degrees)
            current_ra, current_dec = self.get_ra_dec()

            # 2. Robust Cosine Correction
            # Use abs() because cos(x) == cos(-x), but it's safer for mental logic
            # Clamp to 89.99 to allow movement near poles without math errors
            clamped_dec = max(min(abs(current_dec), 89.99), 0.0)

            # Pre-calculate the scale factor.
            # If Dec is 0, scale is 1.0. If Dec is 60, scale is 2.0.
            try:
                secant_dec = 1.0 / math.cos(math.radians(clamped_dec))
            except ZeroDivisionError:
                # Fallback for the literal pole
                secant_dec = 1.0

            delta_ra_corrected = delta_ra * secant_dec

            # 3. Convert degrees to encoder steps
            # We use the corrected RA but the raw Dec
            ra_steps = int(delta_ra_corrected * self.config.encoder['steps_per_deg_ra'])
            dec_steps = int(delta_dec * self.config.encoder['steps_per_deg_dec'])

            # 4. Hardware Communication
            await self._safe_comm(self.comm.shift_mount, ra_steps, dec_steps)

            # 5. Wait for completion
            await self._await_mount_at_position()

            self.state = MountState.IDLE
            self.logger.info("Shift completed.")
        except Exception as e:
            self.logger.error(f"Failed to shift mount: {e}", exc_info=True)
            self.state = MountState.FAULT
        finally:
            self._move_task = None

    async def track_non_sidereal(self, ra_rate : float, dec_rate : float):
        """
        Starts tracking at a custom non-sidereal rate.

        Args:
            ra_rate (float): Tracking rate for Right Ascension in degrees per second.
            dec_rate (float): Tracking rate for Declination in degrees per second.

        Raises:
            ValueError: If the requested rate exceeds the safety limit (2.0 deg/sec).
            Exception: If the tracking command fails to send to the hardware.
        """
        try:
            self.logger.info(f"Starting non-sidereal tracking (RA: {ra_rate}, Dec: {dec_rate})...")
            self.state = MountState.TRACKING

            # Limit tracking rate to 2 degrees per second to prevent hardware strain
            MAX_TRACK_RATE = 1.0
            if abs(ra_rate) > MAX_TRACK_RATE or abs(dec_rate) > MAX_TRACK_RATE:
                raise ValueError(f"Tracking rate exceeds maximum limit of {MAX_TRACK_RATE} deg/sec")

            # Convert deg/sec to steps/sec
            ra_steps_per_sec = -1* ra_rate * self.config.encoder['steps_per_deg_ra']
            dec_steps_per_sec = dec_rate * self.config.encoder['steps_per_deg_dec']

            await self._safe_comm(self.comm.track_mount, ra_steps_per_sec, dec_steps_per_sec)

            self.logger.info("Mount is now tracking at non-sidereal rate.")
        except Exception as e:
            self.state = MountState.FAULT
            self.logger.error(f"Failed to start non-sidereal tracking: {e}")
            raise

    async def update_offsets(self, delta_ra_deg :float, delta_dec_deg : float):
        """
        Updates the software-level coordinate offsets.

        Args:
            delta_ra_deg (float): The offset to apply to Right Ascension in degrees.
            delta_dec_deg (float): The offset to apply to Declination in degrees.
        """
        self.ra_offset_deg = delta_ra_deg
        self.dec_offset_deg = delta_dec_deg
        self.logger.info(f"Offsets updated to RA: {delta_ra_deg}, Dec: {delta_dec_deg}")

    async def get_offsets(self) -> tuple[float, float]:
        """
        Retrieves the current software-level coordinate offsets.

        Returns:
            tuple[float, float]: A tuple containing (ra_offset_deg, dec_offset_deg).
        """
        return self.ra_offset_deg, self.dec_offset_deg

    async def get_ra_dec(self):
        """
        Returns the current RA and Dec of the telescope in degrees.
        Calculated using the current encoder positions and the coordinate module,
        excluding any software offsets.

        Returns:
            tuple: (ra_deg, dec_deg) as floats.
        """

        return self.coord.enc_to_radec(self.current_positions['ra_enc'], self.current_positions['dec_enc'])

    async def _attempt_recovery(self):
        self.logger.info("Attempting servo and mount recovery...")
        max_retry_attempts = 3
        try:

            await self._safe_comm(self.comm.init_mount)

        except Exception as e:
            self.logger.error(f"Recovery failed after {1} attempts: {e}")

    async def _await_encoder_stop(self, tolerance=100, timeout=60):
        """
        Wait until encoders stay within tolerance for 5 seconds or timeout.

        Args:
            tolerance (int): Maximum allowed encoder count change between samples.
            timeout (int): Maximum time in seconds to wait for stability.

        Raises:
            TimeoutError: If the mount does not stabilize within the timeout period.
        """
        start_time = asyncio.get_event_loop().time()
        stable_start_time = None

        last_ra = self.current_positions["ra_enc"]
        last_dec = self.current_positions["dec_enc"]

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            curr_ra = self.current_positions["ra_enc"]
            curr_dec = self.current_positions["dec_enc"]

            if abs(curr_ra - last_ra) <= tolerance and abs(curr_dec - last_dec) <= tolerance:
                if stable_start_time is None:
                    stable_start_time = asyncio.get_event_loop().time()
                elif (asyncio.get_event_loop().time() - stable_start_time) >= 5.0:
                    return
            else:
                stable_start_time = None
                last_ra, last_dec = curr_ra, curr_dec

            await asyncio.sleep(0.1)
        raise TimeoutError("Mount failed to stop within timeout period.")

    async def _await_mount_at_position(self, timeout=180, tolerance=100):
        """
        Wait until current encoder positions match target positions within tolerance.

        Args:
            timeout (int): Maximum time in seconds to wait for the mount to reach the target.
            tolerance (int): Maximum allowed difference between actual and target encoder counts.

        Raises:
            TimeoutError: If the mount does not reach the target position within the
                          specified timeout period.
        """
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            ra_diff = abs(self.current_positions["ra_enc"] - self.current_positions["ra_target_enc"])
            dec_diff = abs(self.current_positions["dec_enc"] - self.current_positions["dec_target_enc"])

            if ra_diff <= tolerance and dec_diff <= tolerance:
                return

            await asyncio.sleep(0.2)

        raise TimeoutError(f"Mount failed to reach target position within {timeout}s ")

    async def _safe_comm(self, func, *args, **kwargs):
        """Standard lock wrapper to prevent serial collision."""
        async with self.serial_lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def _status_loop(self):
        while True:
            try:

                ra_target, ra_actual = await self._safe_comm(self.comm.get_encoder_position, 0)
                dec_target, dec_actual = await self._safe_comm(self.comm.get_encoder_position, 1)

                ra_axis_status = await self._safe_comm(self.comm.get_axis_status_bits, 0)
                dec_axis_status = await self._safe_comm(self.comm.get_axis_status_bits, 1)

                self.current_positions = {
                    "ra_enc": ra_actual, "ra_target_enc": ra_target,
                    "dec_enc": dec_actual, "dec_target_enc": dec_target,
                }

                if ra_axis_status['any_error'] or dec_axis_status['any_error']:
                    self.state = MountState.FAULT

            except Exception as e:
                self.logger.error(f"Status Loop Error: {e}")

            await asyncio.sleep(0.1)
