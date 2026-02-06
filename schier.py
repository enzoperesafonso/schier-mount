import asyncio
import logging
from enum import Enum, auto

from comm import MountComm
from configuration import MountConfig


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

        self.config = MountConfig()
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

    async def disconnect_mount(self):
        pass

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
            await self._await_encoder_stop(tolerance=10, timeout=120)

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

    async def track_sidereal(self):
        sidereal_rate_deg_per_sec = 0.004178
        pass

    async def shift_mount(self):
        pass

    async def slew_mount(self):
        pass

    async def track_non_sidereal(self, ra_rat : float, dec_rate : float):
        pass

    def _attempt_recovery(self):
        pass

    async def _await_encoder_stop(self, tolerance=10, timeout=60):
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

            await asyncio.sleep(0.2)
        raise TimeoutError("Mount failed to stop within timeout period.")

    async def _await_mount_at_position(self, timeout=180, tolerance=10):
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
                # Acquire the lock ONCE for the whole group of commands
                async with self.serial_lock:
                    # We use to_thread inside the lock so no one else can
                    # interrupt the serial line until all 4 calls finish.
                    ra_target, ra_actual = await asyncio.to_thread(self.comm.get_encoder_position, 0)
                    dec_target, dec_actual = await asyncio.to_thread(self.comm.get_encoder_position, 1)

                    ra_axis_status = await asyncio.to_thread(self.comm.get_axis_status_bits, 0)
                    dec_axis_status = await asyncio.to_thread(self.comm.get_axis_status_bits, 1)

                    self.current_positions = {
                        "ra_enc": ra_actual, "ra_target_enc": ra_target,
                        "dec_enc": dec_actual, "dec_target_enc": dec_target,
                    }

                    if ra_axis_status['any_error'] or dec_axis_status['any_error']:
                        self.state = MountState.FAULT

            except Exception as e:
                self.logger.error(f"Status Loop Error: {e}")

            await asyncio.sleep(0.5)  # Slowed down slightly for stability
