import asyncio
import logging
from enum import Enum, auto

import comm

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
        self.comm = comm.MountComm()

        self._status_task = None
        self.serial_lock = asyncio.Lock()

        self.current_positions = {
            "ra_enc": 0, "ra_target_enc": 0,
            "dec_enc": 0, "dec_target_enc": 0,
        }

        self.state = MountState.UNKNOWN

    def init_mount(self):

        self.comm.init_mount()
        self.state = MountState.PARKED

    def calibrate_mount(self):
        pass

    def stop_mount(self):
        pass

    def park_mount(self):
        pass

    def unpark_mount(self):
        pass

    def standby_mount(self):
        pass

    def track_sidereal(self):
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

                ra_target, ra_actual = await self._safe_comm(self.comm.get_encoder_position, 0)
                dec_target, dec_actual = await self._safe_comm(self.comm.get_encoder_position, 1)

                ra_axis_status = await self._safe_comm(self.comm.get_axis_status_bits,0)
                dec_axis_status = await self._safe_comm(self.comm.get_axis_status_bits,1)

                self.current_positions = {
                    "ra_enc": ra_actual, "ra_target_enc": ra_target,
                    "dec_enc": dec_actual, "dec_target_enc": dec_target,
                }

                if ra_axis_status['any_error'] or dec_axis_status['any_error']:
                    self.state = MountState.FAULT

            except Exception as e:
                self.logger.error(f"Status Loop Error: {e}")

            await asyncio.sleep(0.2)  # 5Hz Polling
