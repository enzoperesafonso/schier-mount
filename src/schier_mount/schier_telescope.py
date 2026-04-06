"""
Pyobs module for controlling Schier mounts (ROTSEIIc).
"""
import asyncio
import logging
from typing import Any, Tuple, Optional
from pyobs.modules.telescope import BaseTelescope
from pyobs.interfaces import IPointingRaDec, IOffsetsRaDec, ICalibrate
from pyobs.utils.enums import MotionStatus
from pyobs.modules import timeout

from .schier import SchierMount, MountState

MOUNT_STATE_MAPPING = {
    MountState.IDLE: MotionStatus.IDLE,
    MountState.SLEWING: MotionStatus.SLEWING,
    MountState.TRACKING: MotionStatus.TRACKING,
    MountState.PARKING: MotionStatus.PARKING,
    MountState.PARKED: MotionStatus.PARKED,
    MountState.HOMING: MotionStatus.INITIALIZING,
    MountState.RECOVERING: MotionStatus.INITIALIZING,
    MountState.FAULT: MotionStatus.ERROR,
    MountState.UNKNOWN: MotionStatus.UNKNOWN
}

log = logging.getLogger(__name__)


class SchierTelescope(BaseTelescope, IPointingRaDec, IOffsetsRaDec, ICalibrate):
    """
    Custom Pyobs module for the ROTSEIIc Telescope Mount.

    This class implements the pyobs telescope interfaces to communicate with
    the Schier mount driver, providing methods for pointing, offsetting,
    and synchronization.
    """

    def __init__(self, **kwargs: Any):
        BaseTelescope.__init__(self, **kwargs, motion_status_interfaces=["ITelescope"])

        self._driver = SchierMount()

        # Start a background task to keep pyobs updated
        self.add_background_task(self._update_status_loop)

    async def open(self) -> None:
        await BaseTelescope.open(self)


    async def _move_altaz(self, alt: float, az: float, abort_event: asyncio.Event) -> None:
        pass

    async def is_ready(self, **kwargs: Any) -> bool:
        return True

    async def stop_motion(self, device: Optional[str] = None, **kwargs: Any) -> None:
        pass


    async def calibrate(self, **kwargs: Any) -> None:
        """
        Calibrate the mount i.e. home.
        """
        await self._driver.home_mount()

    async def init(self, **kwargs: Any) -> None:
        """
        Unparks telescope NOT INITIALISES MOUNT... i.e. move out of parked status and slew to standby pos.
        """
        await self._driver.standby_mount()

    async def park(self, **kwargs: Any) -> None:
        """
        Park the telescope.
        """
        await self._driver.park_mount()


    async def get_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """
        Get the current RA and Dec.

        Returns:
            Tuple of (RA, Dec) in degrees.
        """

        ra, dec = await self._driver.get_ra_dec()

        return ra, dec

    async def _move_radec(self, ra: float, dec: float, abort_event: asyncio.Event) -> None:
        """
        Move the telescope to the given RA and Dec.

        Args:
            ra: Right Ascension in degrees.
            dec: Declination in degrees.
            abort_event: Event to signal movement abort.
        """
        pass

    async def set_offsets_radec(self, dra: float, ddec: float, **kwargs: Any) -> None:
        """
        Set RA and Dec offsets.

        Args:
            dra: RA offset in degrees.
            ddec: Dec offset in degrees.
        """
        pass


    async def get_offsets_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """
        Get current RA and Dec offsets.

        Returns:
            Tuple of (dra, ddec) in degrees.
        """
        pass

    async def _update_status_loop(self):
        """Polls the driver and pushes status changes to pyobs."""
        while True:
            try:
                # 1. Get state from driver
                current_mount_state = self._driver.get_state()

                # 2. Map to pyobs MotionStatus
                pyobs_status = MOUNT_STATE_MAPPING.get(
                    current_mount_state,
                    MotionStatus.UNKNOWN
                )

                # 3. Notify pyobs (it only sends an event if the status actually changed)
                await self._change_motion_status(pyobs_status)

            except Exception as e:
                log.error(f"Error reading mount status: {e}")
                await self._change_motion_status(MotionStatus.ERROR)

            # Poll every 500ms for responsive UI/automation
            await asyncio.sleep(0.5)

    async def get_motion_status(self, device: Optional[str] = None, **kwargs: Any) -> MotionStatus:
        """Required by IMotion interface: returns the current pyobs status."""
        return await BaseTelescope.get_motion_status(self, device)


__all__ = ["SchierTelescope"]
