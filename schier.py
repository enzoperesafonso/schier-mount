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
            "ra_enc": 0,
            "dec_enc": 0,
        }

        self.ra_offset_deg = 0.0
        self.dec_offset_deg = 0.0

        self.config = MountConfig()
        self.coord = MountCoordinates(config=self.config)
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
            self.state = MountState.UNKNOWN
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

            # Use safe_comm to send the init command to ensure we are in stop condition before homing!
            await self._safe_comm(self.comm.init_mount())

            # give it a chance to get into stop ...
            await asyncio.sleep(5.0)

            # Use safe_comm to send the homing command
            await self._safe_comm(self.comm.home_mount)

            self.logger.debug("Homing command sent, waiting for encoders to stabilize...")
            await self._await_encoder_stop(tolerance=100, timeout=120)

            await self._safe_comm(self.comm.zero_mount)

            # send to home position so we don't trigger any near limit errors
            await self.park_mount()

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
            #await self._await_encoder_stop(tolerance=100, timeout=120)
            await self._await_mount_at_position()

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
            #await self._await_encoder_stop(tolerance=100, timeout=120)

            await self._await_mount_at_position()

            self.state = MountState.IDLE
            self.logger.info("Mount moved to standby pos.")

        except Exception as e:
            logging.error(f"Failed to move mount: {e}")
        finally:
            self._move_task = None

    async def slew_mount(self, ha_deg : float, dec_deg : float, apply_offsets=False):
        """
        Slews the mount to the specified HA and Dec coordinates.

        Args:
            ha_deg (float): Target Hour Angle in degrees.
            dec_deg (float): Target Declination in degrees.
            apply_offsets (bool): Whether to apply software RA/Dec offsets.
        """
        try:
            self.logger.info(f"Slewing to HA: {ha_deg}, Dec: {dec_deg} (offsets={apply_offsets})...")
            self.state = MountState.SLEWING
            self._move_task = asyncio.current_task()

            # 1. Apply software offsets if requested
            if apply_offsets:
                # HA = LST - RA => dHA = -dRA
                target_ha = ha_deg - self.ra_offset_deg
                target_dec = dec_deg + self.dec_offset_deg
            else:
                target_ha = ha_deg
                target_dec = dec_deg

            # 2. Convert to encoder steps
            ra_steps, dec_steps = self.coord.hadec_to_enc(target_ha, target_dec)

            # 3. Send hardware command
            self.logger.info(f"Slew Command: target HA={target_ha:.4f} ({int(ra_steps)} enc), target Dec={target_dec:.4f} ({int(dec_steps)} enc)")
            await self._safe_comm(self.comm.slew_mount, int(ra_steps), int(dec_steps))

            # 4. Wait for completion
            await self._await_mount_at_position(timeout=120)

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
        configuration and orientation. In the Southern Hemisphere:
        - Normal Mode: Negative RA motor direction tracks stars.
        - Below-Pole: Positive RA motor direction tracks stars.

        Transitions the mount state to TRACKING.
        """
        try:
            self.logger.info("Starting sidereal tracking...")
            self.state = MountState.TRACKING

            ra_enc = self.current_positions["ra_enc"]
            dec_enc = self.current_positions["dec_enc"]
            is_below_pole = self.coord.is_below_pole(ra_enc, dec_enc)

            # Sidereal rate in degrees per second
            sidereal_deg_per_sec = 0.004178

            # Decide sign based
            # on orientation
            direction = 1 if is_below_pole else -1
            sidereal_rate_steps_per_sec = direction * sidereal_deg_per_sec * self.config.encoder['steps_per_deg_ra']

            self.logger.info(f"Tracking mode: {'Below-Pole' if is_below_pole else 'Normal'} | Rate: {sidereal_rate_steps_per_sec:.4f} steps/s")

            await self._safe_comm(self.comm.track_mount, sidereal_rate_steps_per_sec, 0.0)

            self.logger.info("Mount is now tracking at sidereal rate.")
        except Exception as e:
            self.state = MountState.FAULT
            self.logger.error(f"Failed to start sidereal tracking: {e}")
            raise

    async def shift_mount(self, delta_ra: float, delta_dec: float):
        """
        Shifts the mount by a relative amount of degrees in RA and Dec.
        
        Args:
            delta_ra (float): The relative shift in Right Ascension (degrees).
            delta_dec (float): The relative shift in Declination (degrees).
        """
        try:
            self.state = MountState.SLEWING
            self._move_task = asyncio.current_task()

            # HA = LST - RA => dHA = -dRA
            # If we want to increase RA, we must move the mount West (decrease HA).
            # Negative steps per second move the mount West (increasing HA).
            # So dHA = -dRA.
            # Steps = dHA * (-steps_per_deg_ra) = (-dRA) * (-steps_per_deg_ra) = dRA * steps_per_deg_ra.
            
            ra_steps = int(delta_ra * self.config.encoder['steps_per_deg_ra'])
            dec_steps = int(delta_dec * self.config.encoder['steps_per_deg_dec'])

            await self._safe_comm(self.comm.shift_mount, ra_steps, dec_steps)
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
        """
        try:
            self.logger.info(f"Starting non-sidereal tracking (RA rate: {ra_rate}, Dec rate: {dec_rate})...")
            self.state = MountState.TRACKING

            ra_enc = self.current_positions["ra_enc"]
            dec_enc = self.current_positions["dec_enc"]
            is_below_pole = self.coord.is_below_pole(ra_enc, dec_enc)

            # HA_rate = Sidereal_rate - RA_rate
            sidereal_rate = 0.004178 
            ha_rate = sidereal_rate - ra_rate

            # RA Direction: Normal=-1, Below-Pole=+1 (relative to HA rate)
            # Dec Direction: Normal=+1, Below-Pole=-1 (relative to Dec rate)
            ra_direction = 1 if is_below_pole else -1
            dec_direction = -1 if is_below_pole else 1

            ra_steps_per_sec = ra_direction * ha_rate * self.config.encoder['steps_per_deg_ra']
            dec_steps_per_sec = dec_direction * dec_rate * self.config.encoder['steps_per_deg_dec']

            self.logger.info(f"Tracking mode: {'Below-Pole' if is_below_pole else 'Normal'} | RA Rate: {ra_steps_per_sec:.4f} steps/s | Dec Rate: {dec_steps_per_sec:.4f} steps/s")

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

    async def slew_mount_ra_dec(self, ra_deg : float, dec_deg : float ):
        """
        Slews the mount to the specified RA and Dec coordinates.
        """
        target_ra = ra_deg + self.ra_offset_deg
        target_dec = dec_deg + self.dec_offset_deg
        ha_deg = self.coord.ra_to_ha(target_ra)
        await self.slew_mount(ha_deg, target_dec, apply_offsets=False)

    async def get_ra_dec(self):
        """
        Returns the current RA and Dec of the telescope in degrees.
        """
        ha_deg, dec_deg = await self.get_ha_dec()
        ra_deg = self.coord.ha_to_ra(ha_deg)
        return ra_deg, dec_deg

    async def get_ha_dec(self):
        """
        Returns the current HA and Dec of the telescope in degrees.
        Calculated using the current encoder positions and the coordinate module,
        excluding any software offsets.

        Returns:
            tuple: (ha_deg, dec_deg) as floats.
        """
        ra_enc = self.current_positions["ra_enc"]
        dec_enc = self.current_positions["dec_enc"]
        
        ha_deg, dec_deg = self.coord.enc_to_hadec(ra_enc, dec_enc)
        
        return ha_deg, dec_deg

    async def attempt_recovery(self):
        self.logger.info("Attempting servo and mount recovery...")
        try:

            await self._safe_comm(self.comm.init_mount)

        except Exception as e:
            self.logger.error(f"Recovery failed after {1} attempts: {e}")

    async def _await_encoder_stop(self, tolerance=100, timeout=60):
        """
        Wait until encoders stay within tolerance for 5 seconds or timeout.
        Immediately raises an error if the mount enters a FAULT state.
        """
        start_time = asyncio.get_event_loop().time()
        stable_start_time = None

        last_ra = self.current_positions["ra_enc"]
        last_dec = self.current_positions["dec_enc"]

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            if self.state == MountState.FAULT:
                raise RuntimeError("Mount entered FAULT state during movement.")

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
        Immediately raises an error if the mount enters a FAULT state.
        """
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            if self.state == MountState.FAULT:
                raise RuntimeError("Mount entered FAULT state during slew.")

            ra_diff = abs(self.current_positions["ra_enc"] - self.comm.ra_target_enc)
            dec_diff = abs(self.current_positions["dec_enc"] - self.comm.dec_target_enc)

            if ra_diff <= tolerance and dec_diff <= tolerance:
                return

            await asyncio.sleep(0.1)

        raise TimeoutError(f"Mount failed to reach target position within {timeout}s ")

    async def _safe_comm(self, func, *args, **kwargs):
        """Standard lock wrapper to prevent serial collision."""
        async with self.serial_lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def _status_loop(self):
        while True:
            try:

                _, ra_actual = await self._safe_comm(self.comm.get_encoder_position, 0)
                _, dec_actual = await self._safe_comm(self.comm.get_encoder_position, 1)

                ra_axis_status = await self._safe_comm(self.comm.get_axis_status_bits, 0)
                dec_axis_status = await self._safe_comm(self.comm.get_axis_status_bits, 1)

                self.current_positions = {
                    "ra_enc": ra_actual,
                    "dec_enc": dec_actual,
                }

                # 2. Hardware Status Check
                if ra_axis_status['any_error'] or dec_axis_status['any_error']:
                    if self.state != MountState.FAULT:
                        self.logger.error(f"Hardware fault detected (RA: {ra_axis_status['raw_word1']:04x}, Dec: {dec_axis_status['raw_word1']:04x})! Stopping.")
                        self.state = MountState.FAULT
                        await self.stop_mount()

                # 3. Software Limit Guard
                # Check if we are approaching software limits while moving
                if self.state in [MountState.TRACKING, MountState.SLEWING]:
                    buffer_deg = 0.5  # 0.5 degree safety buffer

                    ra_min = self.config.limits['ra_min'] + buffer_deg
                    ra_max = self.config.limits['ra_max'] - buffer_deg
                    dec_min = self.config.limits['dec_min'] + buffer_deg
                    dec_max = self.config.limits['dec_max'] - buffer_deg

                    # Convert actual positions back to mechanical degrees
                    mech_ra = (ra_actual - self.config.encoder['zeropt_ra']) / self.config.encoder['steps_per_deg_ra']
                    mech_dec = (dec_actual - self.config.encoder['zeropt_dec']) / self.config.encoder['steps_per_deg_dec']

                    if not (ra_min <= mech_ra <= ra_max) or not (dec_min <= mech_dec <= dec_max):
                        self.logger.warning(f"Software limit approach detected (RA: {mech_ra:.2f}, Dec: {mech_dec:.2f})! Emergency stop.")
                        await self.stop_mount()

            except Exception as e:
                self.logger.error(f"Status Loop Error: {e}")
                if self.state != MountState.FAULT:
                    self.state = MountState.FAULT
                    # Try to stop hardware if possible
                    try:
                        await self.stop_mount()
                    except:
                        pass

            await asyncio.sleep(0.01)
