"""
ROTSE-III Telescope Module for pyobs.

Provides a pyobs interface to control the ROTSE-III fork-mounted equatorial telescope
using the async telescope driver.
"""

import asyncio
import logging
from typing import Any, Dict, Tuple, Optional
import math
import sys
from pathlib import Path

# Add parent directory to path for telescope driver imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from astropy.coordinates import SkyCoord, ICRS, AltAz
import astropy.units as u

from pyobs.interfaces import IPointingRaDec, IOffsetsRaDec
from pyobs.modules.telescope.basetelescope import BaseTelescope
from pyobs.modules import timeout
from pyobs.utils.enums import MotionStatus
from pyobs.utils.time import Time
from pyobs.utils import exceptions as exc

# Import our async telescope driver
from async_telescope_driver import AsyncTelescopeDriver, SlewMode, InitializationResult
from state import MountState, TrackingMode, PierSide

log = logging.getLogger(__name__)


class ROTSE3Telescope(BaseTelescope, IPointingRaDec, IOffsetsRaDec):
    """
    ROTSE-III Fork-Mounted Equatorial Telescope for pyobs.
    
    This module provides a pyobs interface to the ROTSE-III telescope using
    the async telescope driver. It handles coordinate conversions between
    RA/Dec (J2000) and HA/Dec (apparent) coordinate systems.
    
    Features:
    - Full pyobs integration with async/await support
    - RA/Dec to HA/Dec conversion using observer location
    - Offset tracking and accumulation
    - Motion status synchronization
    - Emergency stop and recovery capabilities
    - Comprehensive error handling and logging
    """

    __module__ = "pyobs.modules.telescope"

    def __init__(
        self,
        telescope: Optional[Dict[str, Any]] = None,
        slew_timeout: float = 300.0,
        position_tolerance: float = 0.01,  # degrees
        **kwargs: Any,
    ):
        """
        Initialize ROTSE-III telescope.
        
        Args:
            telescope: Telescope configuration dictionary containing:
                - serial: Serial communication settings (port, baudrate, timeout, etc.)
                - limits: Encoder limits (ha_positive, dec_negative, etc.)
                - ranges: Encoder ranges (ha_encoder_range, dec_encoder_range)
                - coordinates: Observer location and coordinate system parameters
                - motion: Motion parameters for different slew modes
                - tracking: Tracking parameters
                - safety: Safety parameters and limits
                - monitoring: Status monitoring settings
            slew_timeout: Maximum time for slew operations (seconds)
            position_tolerance: Position tolerance for slew completion (degrees)
            **kwargs: Additional arguments for BaseTelescope
        """
        BaseTelescope.__init__(self, **kwargs)
        
        # Store configuration
        self._telescope_config = telescope or {}
        self._slew_timeout = slew_timeout
        self._position_tolerance = position_tolerance
        
        # Initialize telescope driver (will be created in open())
        self._telescope: Optional[AsyncTelescopeDriver] = None
        
        # Offset tracking
        self._ra_offset = 0.0  # degrees
        self._dec_offset = 0.0  # degrees
        
        # Status monitoring
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_motion_status = MotionStatus.UNKNOWN
        
        log.info("ROTSE-III telescope module initialized")

    def _create_telescope_driver(self) -> 'AsyncTelescopeDriver':
        """
        Create telescope driver using embedded configuration.
        
        Returns:
            Configured AsyncTelescopeDriver instance
            
        Raises:
            InitError: If configuration is invalid or driver creation fails
        """
        try:
            from config import TelescopeConfig
            
            # Create TelescopeConfig from embedded dictionary
            if not self._telescope_config:
                raise exc.InitError("No telescope configuration provided")
            
            # Create config object from dictionary
            telescope_config = TelescopeConfig.from_dict(self._telescope_config)
            
            # Create and return async telescope driver
            return AsyncTelescopeDriver(telescope_config)
            
        except Exception as e:
            log.error(f"Failed to create telescope driver: {e}")
            raise exc.InitError(f"Telescope driver creation failed: {e}")

    async def open(self) -> None:
        """Open telescope module and establish connection."""
        await BaseTelescope.open(self)
        
        try:
            # Create telescope driver with embedded configuration
            log.info("Creating telescope driver connection")
            self._telescope = self._create_telescope_driver()
            
            # Connect to telescope
            if not await self._telescope.connect():
                raise exc.InitError("Failed to connect to ROTSE-III telescope")
            
            log.info("Successfully connected to ROTSE-III telescope")
            
            # Start status monitoring
            self._monitor_task = asyncio.create_task(self._status_monitor())
            
            # Set initial motion status
            await self._update_motion_status()
            
        except Exception as e:
            log.error(f"Failed to open ROTSE-III telescope: {e}")
            await self.close()
            raise
    
    async def close(self) -> None:
        """Close telescope module and cleanup."""
        log.info("Closing ROTSE-III telescope module")
        
        # Stop status monitoring
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        # Disconnect telescope
        if self._telescope:
            await self._telescope.disconnect()
            self._telescope = None
        
        await BaseTelescope.close()

    async def _move_radec(self, ra: float, dec: float, abort_event: asyncio.Event) -> None:
        """
        Move telescope to RA/Dec coordinates (J2000).
        
        This method converts RA/Dec (J2000) to HA/Dec (apparent) and commands
        the telescope to slew to the target position.
        
        Args:
            ra: Right Ascension in degrees (J2000)
            dec: Declination in degrees (J2000) 
            abort_event: Event that gets triggered when movement should be aborted
            
        Raises:
            MoveError: If telescope cannot be moved
        """
        if not self._telescope:
            raise exc.MoveError("Telescope not connected")
        
        if not self._telescope.is_initialized():
            raise exc.MoveError("Telescope not initialized")
        
        log.info(f"Moving to RA={ra:.5f}°, Dec={dec:.5f}° (J2000)")
        
        try:
            # Convert RA/Dec (J2000) to current HA/Dec
            ra_with_offset = ra + self._ra_offset
            dec_with_offset = dec + self._dec_offset
            
            ha, dec_apparent = self._radec_to_hadec(ra_with_offset, dec_with_offset)
            
            log.info(f"Converted to HA={ha:.5f}h, Dec={dec_apparent:.5f}° (apparent)")
            
            # Start telescope slew
            success = await self._telescope.slew_to_coordinates(ha, dec_apparent, SlewMode.NORMAL)
            if not success:
                raise exc.MoveError("Failed to start telescope slew")
            
            # Wait for slew completion with abort handling
            await self._wait_for_slew_completion(abort_event)
            
        except Exception as e:
            log.error(f"Move to RA/Dec failed: {e}")
            if isinstance(e, exc.PyObsError):
                raise
            else:
                raise exc.MoveError(f"Telescope move failed: {e}")
    
    async def _move_altaz(self, alt: float, az: float, abort_event: asyncio.Event) -> None:
        """
        Move telescope to Alt/Az coordinates.
        
        Converts Alt/Az to RA/Dec and then to HA/Dec for telescope control.
        
        Args:
            alt: Altitude in degrees
            az: Azimuth in degrees
            abort_event: Event that gets triggered when movement should be aborted
            
        Raises:
            MoveError: If telescope cannot be moved
        """
        if not self.observer:
            raise exc.MoveError("No observer location configured")
        
        log.info(f"Moving to Alt={alt:.5f}°, Az={az:.5f}°")
        
        try:
            # Convert Alt/Az to RA/Dec (J2000)
            altaz_coord = SkyCoord(
                alt=alt * u.deg, 
                az=az * u.deg,
                obstime=Time.now(),
                location=self.observer.location,
                frame='altaz'
            )
            radec_coord = altaz_coord.icrs
            
            # Use the RA/Dec move method
            await self._move_radec(
                radec_coord.ra.degree, 
                radec_coord.dec.degree, 
                abort_event
            )
            
        except Exception as e:
            log.error(f"Move to Alt/Az failed: {e}")
            if isinstance(e, exc.PyObsError):
                raise
            else:
                raise exc.MoveError(f"Alt/Az move failed: {e}")

    async def _wait_for_slew_completion(self, abort_event: asyncio.Event) -> None:
        """
        Wait for telescope slew to complete.
        
        Args:
            abort_event: Event that gets triggered when movement should be aborted
            
        Raises:
            MoveError: If slew times out or fails
        """
        if not self._telescope:
            raise exc.MoveError("Telescope not connected")
        
        start_time = asyncio.get_event_loop().time()
        
        while True:
            # Check for abort
            if abort_event.is_set():
                log.info("Slew aborted by user")
                await self._telescope.stop()
                raise exc.MoveError("Slew aborted")
            
            # Check for timeout
            if (asyncio.get_event_loop().time() - start_time) > self._slew_timeout:
                log.error(f"Slew timeout after {self._slew_timeout} seconds")
                await self._telescope.stop()
                raise exc.MoveError("Slew timeout")
            
            # Get current telescope state
            state = self._telescope.status.state
            
            # Check if slew completed
            if state == MountState.IDLE or state == MountState.TRACKING:
                log.info("Slew completed successfully")
                return
            elif state == MountState.ERROR:
                raise exc.MoveError("Telescope error during slew")
            elif state not in [MountState.SLEWING, MountState.PARKING]:
                log.warning(f"Unexpected telescope state during slew: {state.value}")
            
            # Wait before checking again
            await asyncio.sleep(0.5)

    def _radec_to_hadec(self, ra: float, dec: float) -> Tuple[float, float]:
        """
        Convert RA/Dec (J2000) to HA/Dec (apparent).
        
        Args:
            ra: Right Ascension in degrees (J2000)
            dec: Declination in degrees (J2000)
            
        Returns:
            Tuple of (hour_angle_hours, declination_degrees)
        """
        if not self.observer:
            raise ValueError("No observer location configured")
        
        # Create coordinate object
        radec_coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame=ICRS)
        
        # Convert to current apparent coordinates
        now = Time.now()
        apparent = self.observer.radec_of_date(now, radec_coord)
        
        # Calculate hour angle
        lst = self.observer.local_sidereal_time(now)  # Local Sidereal Time
        hour_angle = lst.hour - apparent.ra.hour
        
        # Normalize hour angle to [-12, +12] hours
        while hour_angle > 12.0:
            hour_angle -= 24.0
        while hour_angle < -12.0:
            hour_angle += 24.0
        
        return hour_angle, apparent.dec.degree

    def _hadec_to_radec(self, ha: float, dec: float) -> Tuple[float, float]:
        """
        Convert HA/Dec (apparent) to RA/Dec (J2000).
        
        Args:
            ha: Hour Angle in hours
            dec: Declination in degrees (apparent)
            
        Returns:
            Tuple of (ra_degrees, dec_degrees) in J2000
        """
        if not self.observer:
            raise ValueError("No observer location configured")
        
        # Calculate current Local Sidereal Time
        now = Time.now()
        lst = self.observer.local_sidereal_time(now)
        
        # Convert HA to RA (apparent)
        ra_apparent = lst.hour - ha
        
        # Normalize RA to [0, 24] hours
        while ra_apparent < 0.0:
            ra_apparent += 24.0
        while ra_apparent >= 24.0:
            ra_apparent -= 24.0
        
        # Create apparent coordinate
        apparent_coord = SkyCoord(
            ra=ra_apparent * u.hour,
            dec=dec * u.deg,
            obstime=now,
            frame='geocentricmeanecliptic'  # Use apparent coordinates
        )
        
        # Convert to J2000
        j2000_coord = apparent_coord.icrs
        
        return j2000_coord.ra.degree, j2000_coord.dec.degree

    async def get_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """
        Get current RA/Dec position (J2000).
        
        Returns:
            Tuple of current RA and Dec in degrees (J2000)
        """
        if not self._telescope:
            raise exc.GeneralError("Telescope not connected")
        
        # Get current HA/Dec from telescope
        ha, dec = self._telescope.get_position()
        
        if ha is None or dec is None:
            raise exc.GeneralError("Cannot determine telescope position")
        
        # Convert HA/Dec to RA/Dec (J2000)
        ra, dec_j2000 = self._hadec_to_radec(ha, dec)
        
        # Remove offsets to get base pointing position
        ra -= self._ra_offset
        dec_j2000 -= self._dec_offset
        
        log.debug(f"Current position: RA={ra:.5f}°, Dec={dec_j2000:.5f}° (J2000)")
        
        return ra, dec_j2000

    async def set_offsets_radec(self, dra: float, ddec: float, **kwargs: Any) -> None:
        """
        Set RA/Dec offsets.
        
        Args:
            dra: RA offset in degrees
            ddec: Dec offset in degrees
        """
        log.info(f"Setting RA/Dec offsets: dRA={dra:.5f}°, dDec={ddec:.5f}°")
        
        # Store the new offsets
        old_ra_offset = self._ra_offset  
        old_dec_offset = self._dec_offset
        
        self._ra_offset = dra
        self._dec_offset = ddec
        
        # If telescope is tracking, apply the offset immediately
        if self._telescope and self._telescope.status.state == MountState.TRACKING:
            try:
                # Get current base position (without old offsets)
                current_ra, current_dec = await self.get_radec()
                
                # Add old offsets back to get base position
                base_ra = current_ra + old_ra_offset
                base_dec = current_dec + old_dec_offset
                
                # Apply new offsets and move
                new_ra = base_ra + self._ra_offset
                new_dec = base_dec + self._dec_offset
                
                log.info(f"Applying offset during tracking: moving to RA={new_ra:.5f}°, Dec={new_dec:.5f}°")
                
                # Convert and slew
                ha, dec_apparent = self._radec_to_hadec(new_ra, new_dec)
                await self._telescope.slew_to_coordinates(ha, dec_apparent, SlewMode.PRECISE)
                
            except Exception as e:
                log.error(f"Failed to apply offset during tracking: {e}")
                # Restore old offsets on failure
                self._ra_offset = old_ra_offset
                self._dec_offset = old_dec_offset
                raise exc.MoveError(f"Failed to apply offsets: {e}")

    async def get_offsets_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """
        Get current RA/Dec offsets.
        
        Returns:
            Tuple with current RA and Dec offsets in degrees
        """
        return self._ra_offset, self._dec_offset

    @timeout(300)
    async def init(self, **kwargs: Any) -> None:
        """
        Initialize telescope.
        
        Raises:
            InitError: If telescope cannot be initialized
        """
        if not self._telescope:
            raise exc.InitError("Telescope not connected")
        
        log.info("Initializing ROTSE-III telescope")
        await self._change_motion_status(MotionStatus.INITIALIZING)
        
        try:
            result: InitializationResult = await self._telescope.initialize()
            
            if result.success:
                log.info(f"Telescope initialized successfully in {result.duration_seconds:.1f}s")
                await self._change_motion_status(MotionStatus.IDLE)
            else:
                raise exc.InitError(f"Telescope initialization failed: {result.message}")
                
        except Exception as e:
            log.error(f"Telescope initialization failed: {e}")
            await self._change_motion_status(MotionStatus.ERROR)
            if isinstance(e, exc.PyObsError):
                raise
            else:
                raise exc.InitError(f"Initialization failed: {e}")

    @timeout(180)
    async def park(self, **kwargs: Any) -> None:
        """
        Park telescope.
        
        Raises:
            ParkError: If telescope cannot be parked
        """
        if not self._telescope:
            raise exc.ParkError("Telescope not connected")
        
        log.info("Parking ROTSE-III telescope")
        await self._change_motion_status(MotionStatus.PARKING)
        
        try:
            # Park at default position (meridian, south)
            success = await self._telescope.park(ha=0.0, dec=-20.0)
            
            if not success:
                raise exc.ParkError("Failed to start parking sequence")
            
            # Wait for parking to complete
            start_time = asyncio.get_event_loop().time()
            timeout = 180.0  # 3 minutes
            
            while True:
                state = self._telescope.status.state
                
                if state == MountState.PARKED:
                    log.info("Telescope parked successfully")
                    await self._change_motion_status(MotionStatus.PARKED)
                    return
                elif state == MountState.ERROR:
                    raise exc.ParkError("Telescope error during parking")
                elif (asyncio.get_event_loop().time() - start_time) > timeout:
                    raise exc.ParkError("Parking timeout")
                
                await asyncio.sleep(1.0)
                
        except Exception as e:
            log.error(f"Telescope parking failed: {e}")
            await self._change_motion_status(MotionStatus.ERROR)
            if isinstance(e, exc.PyObsError):
                raise
            else:
                raise exc.ParkError(f"Parking failed: {e}")

    async def stop_motion(self, device: Optional[str] = None, **kwargs: Any) -> None:
        """
        Stop telescope motion.
        
        Args:
            device: Device to stop (ignored, always stops telescope)
        """
        if not self._telescope:
            return
        
        log.info("Stopping telescope motion")
        
        try:
            await self._telescope.stop()
            await self._change_motion_status(MotionStatus.IDLE)
            
        except Exception as e:
            log.error(f"Failed to stop telescope motion: {e}")

    async def _status_monitor(self) -> None:
        """
        Monitor telescope status and update pyobs motion status.
        
        This background task continuously monitors the telescope state and
        updates the pyobs motion status accordingly.
        """
        log.info("Starting telescope status monitor")
        
        while True:
            try:
                await self._update_motion_status()
                await asyncio.sleep(2.0)  # Update every 2 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in status monitor: {e}")
                await asyncio.sleep(5.0)  # Wait longer on error
        
        log.info("Telescope status monitor stopped")

    async def _update_motion_status(self) -> None:
        """Update pyobs motion status based on telescope state."""
        if not self._telescope:
            return
        
        # Get current telescope state
        mount_state = self._telescope.status.state
        
        # Map telescope state to pyobs motion status
        if mount_state == MountState.DISCONNECTED:
            new_status = MotionStatus.ERROR
        elif mount_state == MountState.IDLE:
            new_status = MotionStatus.IDLE
        elif mount_state == MountState.INITIALIZING:
            new_status = MotionStatus.INITIALIZING
        elif mount_state == MountState.HOMING:
            new_status = MotionStatus.INITIALIZING
        elif mount_state == MountState.SLEWING:
            new_status = MotionStatus.SLEWING
        elif mount_state == MountState.TRACKING:
            new_status = MotionStatus.TRACKING
        elif mount_state == MountState.PARKING:
            new_status = MotionStatus.PARKING
        elif mount_state == MountState.PARKED:
            new_status = MotionStatus.PARKED
        elif mount_state == MountState.STOPPING:
            new_status = MotionStatus.SLEWING  # Transitional state
        elif mount_state == MountState.HALTED:
            new_status = MotionStatus.IDLE
        elif mount_state == MountState.ERROR:
            new_status = MotionStatus.ERROR
        else:
            new_status = MotionStatus.UNKNOWN
        
        # Update status if changed
        if new_status != self._last_motion_status:
            await self._change_motion_status(new_status)
            self._last_motion_status = new_status
            log.debug(f"Motion status updated: {mount_state.value} -> {new_status.value}")

    async def is_ready(self, **kwargs: Any) -> bool:
        """
        Check if telescope is ready for operations.
        
        Returns:
            True if telescope is ready, False otherwise
        """
        if not self._telescope:
            return False
        
        return (
            self._telescope.is_connected() and 
            self._telescope.is_initialized() and
            self._telescope.status.state in [MountState.IDLE, MountState.TRACKING]
        )

    async def get_fits_header_before(
        self, namespaces: Optional[list] = None, **kwargs: Any
    ) -> Dict[str, Tuple[Any, str]]:
        """
        Get FITS headers for telescope status.
        
        Args:
            namespaces: Optional namespace filter
            
        Returns:
            Dictionary containing FITS headers
        """
        # Get base headers from BaseTelescope
        hdr = await BaseTelescope.get_fits_header_before(self, namespaces, **kwargs)
        
        if self._telescope:
            # Add ROTSE-III specific headers
            status = self._telescope.get_status()
            
            # Telescope state
            hdr["TEL-STAT"] = (status['state'], "Telescope mount state")
            hdr["TEL-INIT"] = (status['initialized'], "Telescope initialized")
            
            # Encoder positions
            if status['encoders']['ha'] is not None:
                hdr["TEL-HAENC"] = (status['encoders']['ha'], "HA encoder position [steps]")
            if status['encoders']['dec'] is not None:
                hdr["TEL-DECENC"] = (status['encoders']['dec'], "Dec encoder position [steps]")
            
            # Pier side
            if status['position']['pier_side']:
                hdr["PIERSIDE"] = (status['position']['pier_side'], "Telescope pier side")
            
            # Tracking mode
            hdr["TRACKING"] = (status['tracking_mode'], "Telescope tracking mode")
            
            # Offsets
            hdr["TEL-RAOFF"] = (self._ra_offset, "RA offset [degrees]")
            hdr["TEL-DECOFF"] = (self._dec_offset, "Dec offset [degrees]")
        
        return hdr


__all__ = ["ROTSE3Telescope"]