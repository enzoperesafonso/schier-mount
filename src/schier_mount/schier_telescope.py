"""
Pyobs module for controlling Schier mounts (ROTSEIIc).
"""
import asyncio
import logging
from typing import Any, Tuple, Optional
from pyobs.modules.telescope import BaseTelescope
from pyobs.interfaces import IPointingRaDec, IPointingAltAz, IOffsetsRaDec, ICalibrate, ISyncTarget
from pyobs.utils.enums import MotionStatus
from pyobs.modules import timeout

from schier import SchierMount, MountState

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


class SchierTelescope(BaseTelescope, IPointingRaDec, IOffsetsRaDec, ICalibrate, ISyncTarget):
    """
    Custom Pyobs module for the ROTSEIIc Telescope Mounts.

    This class implements the pyobs telescope interfaces to communicate with
    the Schier mount driver, providing methods for pointing, offsetting,
    and synchronization.
    """

    def __init__(self, **kwargs: Any):
        BaseTelescope.__init__(self, **kwargs, motion_status_interfaces=["ITelescope"])

        # Start a background task to keep pyobs updated
        self.add_background_task(self._update_status_loop)

    async def open(self) -> None:
        await BaseTelescope.open(self)
        # Initialization logic for the driver should go here

    async def calibrate(self, **kwargs: Any) -> None:
        """
        Calibrate the mount.
        """
        pass

    async def init(self, **kwargs: Any) -> None:
        """
        Unparks telescope NOT INITIALISES MOUNT... i.e. move out of parked status and slew to standby pos.
        """
        pass

    async def park(self, **kwargs: Any) -> None:
        """
        Park the telescope.
        """
        pass

    async def get_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """
        Get the current RA and Dec.

        Returns:
            Tuple of (RA, Dec) in degrees.
        """
        pass

    async def _move_radec(self, ra: float, dec: float, abort_event: asyncio.Event) -> None:
        """
        Move the telescope to the given RA and Dec.

        Args:
            ra: Right Ascension in degrees.
            dec: Declination in degrees.
            abort_event: Event to signal movement abortion.
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

    async def sync_target(self, **kwargs: Any) -> None:
        """
        Sync the telescope to a target.
        """
        pass

    async def _update_status_loop(self):
        """Polls the driver and pushes status changes to pyobs."""
        while True:
            try:
                # 1. Get state from your driver
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
