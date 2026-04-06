"""
Pyobs module for controlling Schier mounts (ROTSEIIc).
"""
import asyncio
import logging
from typing import Any, Tuple, Optional
from pyobs.modules.telescope import BaseTelescope
from pyobs.interfaces import IPointingRaDec, IOffsetsRaDec, ICalibrate, IAbortable
from pyobs.utils.enums import MotionStatus
from pyobs.modules import timeout
from pyobs.utils import exceptions as exc

# Import the driver and state enum
from .schier import SchierMount, MountState

# Mapping between driver states and pyobs MotionStatus
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

class SchierTelescope(BaseTelescope, IPointingRaDec, IOffsetsRaDec, ICalibrate, IAbortable):
    """
    Custom Pyobs module for the ROTSEIIc Telescope Mount (Schier Mount).

    This module provides the interface between pyobs and the Schier mount driver.
    It implements movement (RA/Dec), offsetting, homing, and automatic status updates.
    """

    def __init__(self, **kwargs: Any):
        # We tell the MotionStatusMixin that we are tracking the status for the ITelescope interface
        BaseTelescope.__init__(self, **kwargs, motion_status_interfaces=["ITelescope"])

        # Create the hardware driver instance
        self._driver = SchierMount()

        # Start a background task to keep pyobs updated with driver changes
        self.add_background_task(self._update_status_loop)

    async def open(self) -> None:
        """Open module and initialize connection to the mount."""
        await BaseTelescope.open(self)

        # Initialize the mount hardware and its internal loops
        log.info("Initializing Schier Mount driver...")
        await self._driver.init_mount()

    async def stop_motion(self, device: Optional[str] = None, **kwargs: Any) -> None:
        """Emergency stop command."""
        log.warning("Stop motion requested!")
        await self._driver.stop_mount()
        # The abort event is automatically set by the framework if stop_motion is called via the API

    async def abort(self, **kwargs: Any) -> None:
        """Abort the current operation (standard interface)."""
        log.warning("Aborting current operation...")
        await self.stop_motion()

    @timeout(600)
    async def calibrate(self, **kwargs: Any) -> None:
        """Home the mount (Calibrate interface)."""
        log.info("Starting homing sequence...")
        await self._driver.home_mount()

    @timeout(120)
    async def init(self, **kwargs: Any) -> None:
        """Move from parked position to standby (Zenith)."""
        log.info("Moving telescope to standby (init)...")
        await self._driver.standby_mount()

    @timeout(120)
    async def park(self, **kwargs: Any) -> None:
        """Park the telescope."""
        log.info("Parking telescope...")
        await self._driver.park_mount()

    async def get_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """Return the current RA and Dec coordinates in degrees."""
        return await self._driver.get_ra_dec()

    async def set_offsets_radec(self, dra: float, ddec: float, **kwargs: Any) -> None:
        """Apply software offsets for RA and Dec."""
        await self._driver.update_offsets(dra, ddec)
        log.info("Updated offsets: RA=%.5f, Dec=%.5f", dra, ddec)

    async def get_offsets_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """Get the current software offsets."""
        return await self._driver.get_offsets()

    async def _move_radec(self, ra: float, dec: float, abort_event: asyncio.Event) -> None:
        """
        Internal implementation of the RA/Dec move.
        This method handles the actual hardware move and coordinates with pyobs aborts.
        """
        try:
            # 1. Prepare and start the slew task
            log.info("Starting slew to RA=%.5f, Dec=%.5f", ra, dec)
            slew_task = asyncio.create_task(self._driver.slew_mount_ra_dec(ra, dec))
            abort_task = asyncio.create_task(abort_event.wait())

            # 2. Wait for completion or abort
            done, pending = await asyncio.wait(
                {slew_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED
            )

            # 3. Handle user abort
            if abort_event.is_set():
                log.warning("Slew aborted by pyobs. Signaling hardware to stop...")
                await self._driver.stop_mount()
                if not slew_task.done():
                    slew_task.cancel()
                raise exc.AbortedError("Telescope move was aborted.")
            else:
                # Clean up abort task if slew finished first
                abort_task.cancel()

            # 4. Handle move completion (re-raises any driver errors)
            await slew_task

            # 5. Start Sidereal Tracking (expected by pyobs after arrival)
            log.info("Target reached. Engaging sidereal tracking...")
            await self._driver.track_sidereal()

        except Exception as e:
            if isinstance(e, exc.PyObsError):
                raise
            log.exception("Error during telescope slew: %s", e)
            await self._driver.stop_mount()


    async def _update_status_loop(self) -> None:
        """Poll the driver and sync its state to the pyobs framework."""
        while True:
            try:
                # 1. Get current driver state
                current_state = self._driver.get_state()

                # 2. Map to pyobs MotionStatus
                pyobs_status = MOUNT_STATE_MAPPING.get(current_state, MotionStatus.UNKNOWN)

                # 3. Inform the MotionStatusMixin (this sends events only on change)
                await self._change_motion_status(pyobs_status, interface="ITelescope")

            except Exception as e:
                log.error("Failed to sync mount status: %s", e)
                await self._change_motion_status(MotionStatus.ERROR, interface="ITelescope")

            # Poll every 500ms for a responsive UI
            await asyncio.sleep(0.5)

    async def get_motion_status(self, device: Optional[str] = None, **kwargs: Any) -> MotionStatus:
        """Required by IMotion: returns the current pyobs status."""
        return await BaseTelescope.get_motion_status(self, device)

    async def _move_altaz(self, alt: float, az: float, abort_event: asyncio.Event) -> None:
        """Alt/Az moves are handled by BaseTelescope via coordinate conversion if only IPointingRaDec is used."""
        pass

    async def is_ready(self, **kwargs: Any) -> bool:
        """The telescope is ready if it's tracking or positioned."""
        status = await self.get_motion_status()
        return status in [MotionStatus.TRACKING, MotionStatus.IDLE]

__all__ = ["SchierTelescope"]
