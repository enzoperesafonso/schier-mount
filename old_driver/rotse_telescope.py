"""
PyObs module for ROTSE-III telescope control.
Integrates the ROTSE-III telescope driver with the PyObs framework.
"""

from __future__ import annotations
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from astropy.coordinates import SkyCoord, ICRS, FK5, EarthLocation
from astropy.time import Time as AstropyTime
import astropy.units as u

from pyobs.interfaces import IPointingRaDec, IFitsHeaderBefore,IPointingAltAz,IMotion
from pyobs.mixins.fitsnamespace import FitsNamespaceMixin
from pyobs.modules.telescope.basetelescope import BaseTelescope
from pyobs.modules import timeout
from pyobs.utils.enums import MotionStatus
from pyobs.utils.time import Time
from pyobs.utils import exceptions as exc

# Import our telescope driver components
from .utils.telescope_driver import TelescopeDriver, SlewMode, CalibrationData
from .utils.state import MountState, TrackingMode

log = logging.getLogger(__name__)


class RotseTelescope(
    BaseTelescope,
    IPointingRaDec,
    IPointingAltAz,
    IMotion,
    IFitsHeaderBefore,
    FitsNamespaceMixin,
):
    """PyObs module for ROTSE-III telescope control."""

    __module__ = "pyobs.modules.telescope"

    def __init__(
        self,
        device: str = "/dev/ttyS0",
        baudrate: int = 9600,
        calibration_data: Optional[Dict[str, Any]] = None,
        slew_timeout: float = 300.0,
        position_tolerance: int = 50,
        monitoring_interval: float = 1.0,
        location: Optional[Dict[str, float]] = None,
        **kwargs: Any,
    ):
        """Initialize ROTSE-III telescope module.

        Args:
            device: Serial device path (e.g., "/dev/ttyS0")
            baudrate: Serial communication baudrate
            calibration_data: Dictionary containing calibration parameters from YAML
            slew_timeout: Maximum slew time in seconds
            position_tolerance: Position tolerance in encoder steps
            monitoring_interval: Status monitoring interval in seconds
            **kwargs: Additional arguments for BaseTelescope
        """
        BaseTelescope.__init__(self, **kwargs)
        FitsNamespaceMixin.__init__(self, **kwargs)

        # Store configuration
        self._device = device
        self._baudrate = baudrate
        self._calibration_data = calibration_data or {}
        self._location = location or {}
        self._slew_timeout = slew_timeout
        self._position_tolerance = position_tolerance
        self._monitoring_interval = monitoring_interval

        # Telescope driver instance
        self._telescope: Optional[TelescopeDriver] = None

        # Current target coordinates (for tracking)
        self._target_ra: Optional[float] = None
        self._target_dec: Optional[float] = None

        # Current offsets in degrees
        self._offset_ra: float = 0.0
        self._offset_dec: float = 0.0

        # Status monitoring
        self._status_task: Optional[asyncio.Task] = None
        self._last_motion_status = MotionStatus.IDLE

        log.info("ROTSE-III telescope module initialized")

    async def open(self) -> None:
        """Open telescope module and establish connection."""
        await BaseTelescope.open(self)

        try:
            # Get observer location from constructor parameter
            observer_latitude = self._location.get('latitude', -23.2716)
            observer_longitude = self._location.get('longitude', 16.5)
            observer_elevation = self._location.get('elevation', 1800.0)

            self.observer = EarthLocation(
                lat=observer_latitude * u.deg,
                lon=observer_longitude * u.deg,
                height=observer_elevation * u.m
            )
            log.info(f"Observer location set to: lat={observer_latitude}°, lon={observer_longitude}°, alt={observer_elevation}m")

            # Create calibration data object
            if self._calibration_data:
                # Calculate RA steps per degree from HA encoder range
                ha_encoder_range = self._calibration_data.get('ranges', {}).get('ha_encoder_range', 4497505)
                ra_steps_per_degree = ha_encoder_range / 180.0

                # Only use the hard limits that correspond to the home position
                # Never use positive Dec limit or negative RA limit from config - calculate dynamically
                config_limits = self._calibration_data.get('limits', {})
                safe_limits = {
                    'ha_positive': config_limits.get('ha_positive'),  # This is where we home to
                    'dec_negative': config_limits.get('dec_negative'),  # This is where we home to
                    # DO NOT include ha_negative or dec_positive - calculate from home + range
                }

                calibration = CalibrationData(
                    observer_latitude= self._location.get('latitude', -23.2716) ,
                    limits=safe_limits,  # Only use limits that correspond to home positions
                    ranges=self._calibration_data.get('ranges', {}),
                    dec_steps_per_degree=self._calibration_data.get('dec_steps_per_degree', 19408),
                    ra_steps_per_degree=ra_steps_per_degree
                )
            else:
                calibration = None

            # Create telescope driver
            self._telescope = TelescopeDriver(
                device=self._device,
                baudrate=self._baudrate,
                calibration_data=calibration
            )

            # Configure telescope parameters
            if self._telescope:
                self._telescope.slew_timeout = self._slew_timeout
                self._telescope.position_tolerance = self._position_tolerance
                self._telescope.monitoring_interval = self._monitoring_interval

            # Connect to telescope
            if not self._telescope.connect():
                raise exc.InitError("Failed to connect to ROTSE-III telescope")

            log.info("Connected to ROTSE-III telescope")

            # Add callbacks for monitoring
            self._telescope.add_position_callback(self._position_callback)
            self._telescope.add_error_callback(self._error_callback)

            # Start status monitoring
            self._status_task = asyncio.create_task(self._monitor_status())

            # Set initial status
            await self._change_motion_status(MotionStatus.IDLE)

            # Initialize telescope completely when opening (don't wait for init() call)
            # This does full homing and calibration, unlike the manual init() which just points to zenith
            log.info("Auto-initializing telescope during module open (full homing and calibration)")
            try:
                await self._perform_initialization()
            except Exception as e:
                log.warning(f"Auto-initialization failed: {e}")
                # Don't fail module open if initialization fails - allow manual init later

        except Exception as e:
            log.error(f"Failed to open ROTSE-III telescope: {e}")
            raise exc.InitError(f"Could not initialize ROTSE-III telescope: {e}")

    async def close(self) -> None:
        """Close telescope module and disconnect."""
        log.info("Closing ROTSE-III telescope module")

        # Stop status monitoring
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

        # Disconnect telescope
        if self._telescope:
            self._telescope.disconnect()
            self._telescope = None

        await BaseTelescope.close(self)

    async def _monitor_status(self) -> None:
        """Background task to monitor telescope status and update PyObs status."""
        log.info("Starting telescope status monitoring")

        try:
            while True:
                if self._telescope:
                    # Get telescope state
                    mount_state = self._telescope.status.state

                    # Convert to PyObs motion status
                    if mount_state == MountState.SLEWING:
                        new_status = MotionStatus.SLEWING
                    elif mount_state == MountState.TRACKING:
                        new_status = MotionStatus.TRACKING
                    elif mount_state == MountState.PARKING:
                        new_status = MotionStatus.PARKING
                    elif mount_state == MountState.PARKED:
                        new_status = MotionStatus.PARKED
                    elif mount_state == MountState.INITIALIZING:
                        new_status = MotionStatus.INITIALIZING
                    elif mount_state in [MountState.ERROR, MountState.HALTED]:
                        new_status = MotionStatus.ERROR
                    else:
                        new_status = MotionStatus.IDLE

                    # Update status if changed
                    if new_status != self._last_motion_status:
                        await self._change_motion_status(new_status)
                        self._last_motion_status = new_status

                await asyncio.sleep(self._monitoring_interval)

        except asyncio.CancelledError:
            log.info("Status monitoring cancelled")
        except Exception as e:
            log.error(f"Error in status monitoring: {e}")

    def _position_callback(self, ha: float, dec: float) -> None:
        """Callback for position updates from telescope driver."""
        if ha is not None and dec is not None:
            log.debug(f"Position update: HA={ha:.3f}h, Dec={dec:.1f}°")

    def _error_callback(self, error: Exception) -> None:
        """Callback for error notifications from telescope driver."""
        log.error(f"Telescope driver error: {error}")
        # Could trigger PyObs error handling here


    def _ra_dec_to_ha_dec(self, ra: float, dec: float, time: Optional[AstropyTime] = None) -> Tuple[float, float]:
        """Convert RA/Dec to HA/Dec for the telescope.

        Args:
            ra: Right ascension in degrees
            dec: Declination in degrees
            time: Observation time (default: now)

        Returns:
            Tuple of (hour_angle, declination) in hours and degrees
        """
        if time is None:
            time = AstropyTime.now()

        if self.observer is None:
            raise ValueError("No observer defined for coordinate conversion")

        # Create coordinate object
        coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame='icrs', obstime=time)

        # Convert to apparent coordinates (FK5 with current epoch and equinox)
        fk5_frame = FK5(equinox=time)
        apparent = coord.transform_to(fk5_frame)

        # Calculate local sidereal time
        lst = time.sidereal_time('apparent', longitude=self.observer.lon)

        # Calculate hour angle (LST - RA)
        ha_deg = (lst.deg - apparent.ra.deg) % 360.0
        if ha_deg > 180.0:
            ha_deg -= 360.0

        # Convert to hours
        ha_hours = ha_deg / 15.0

        return ha_hours, apparent.dec.deg

    async def _move_radec(self, ra: float, dec: float, abort_event: asyncio.Event) -> None:
        """Move telescope to RA/Dec coordinates.

        Args:
            ra: Right ascension in degrees
            dec: Declination in degrees
            abort_event: Event to abort the move
        """
        if not self._telescope:
            raise exc.MoveError("Telescope not connected")

        try:
            # ALWAYS stop telescope before any movement command to prevent faults
            log.debug("Stopping telescope before movement")
            self._telescope.stop()
            await asyncio.sleep(0.5)  # Brief pause to ensure stop is processed

            # Apply offsets to target coordinates
            ra_with_offset, dec_with_offset = self._apply_offsets_radec(ra, dec)
            log.debug(f"Applied offsets: RA {ra:.3f}° + {self._offset_ra:.3f}° = {ra_with_offset:.3f}°, Dec {dec:.3f}° + {self._offset_dec:.3f}° = {dec_with_offset:.3f}°")

            # Convert RA/Dec to HA/Dec for current time
            ha, dec_apparent = self._ra_dec_to_ha_dec(ra_with_offset, dec_with_offset)

            log.info(f"Converting RA/Dec ({ra_with_offset:.3f}°, {dec_with_offset:.3f}°) to HA/Dec ({ha:.3f}h, {dec_apparent:.3f}°)")

            # Store target for tracking
            self._target_ra = ra
            self._target_dec = dec

            # Starting new slew

            # Start slew
            if not self._telescope.slew_to_ha_dec(ha, dec_apparent, SlewMode.NORMAL):
                raise exc.MoveError("Failed to initiate telescope slew")

            # Wait for slew completion or abort
            while self._telescope.status.state == MountState.SLEWING:
                if abort_event.is_set():
                    log.info("Slew aborted by user")
                    self._telescope.stop()
                    raise exc.MoveError("Slew was aborted")

                await asyncio.sleep(0.5)

            # Check final state
            if self._telescope.status.state == MountState.ERROR:
                raise exc.MoveError("Telescope slew failed")

            log.info("Slew completed successfully")

            # Start tracking the target
            await self._start_tracking()

        except Exception as e:
            log.error(f"Error during move to RA/Dec: {e}")
            raise exc.MoveError(f"Failed to move telescope: {e}")

    def _apply_offsets_radec(self, ra: float, dec: float) -> Tuple[float, float]:
        """Apply RA/Dec offsets to target coordinates.

        Args:
            ra: Right ascension in degrees
            dec: Declination in degrees

        Returns:
            Tuple of (ra_with_offset, dec_with_offset) in degrees
        """
        # Apply RA offset (note: RA offset needs cos(dec) correction for proper sky motion)
        ra_offset_corrected = self._offset_ra / math.cos(math.radians(dec)) if dec != 90.0 else 0.0
        ra_with_offset = (ra + ra_offset_corrected) % 360.0

        # Apply Dec offset (direct addition)
        dec_with_offset = dec + self._offset_dec

        # Clamp declination to valid range
        dec_with_offset = max(-89.999, min(89.999, dec_with_offset))

        return ra_with_offset, dec_with_offset

    async def _move_altaz(self, alt: float, az: float, abort_event: asyncio.Event) -> None:
        """Move telescope to Alt/Az coordinates.

        Note: ROTSE-III is an equatorial mount, so this converts Alt/Az to RA/Dec
        and then uses the normal RA/Dec movement method.

        Args:
            alt: Altitude in degrees
            az: Azimuth in degrees
            abort_event: Event to abort the move
        """
        if not self._telescope:
            raise exc.MoveError("Telescope not connected")

        try:
            # ALWAYS stop telescope before any movement command to prevent faults
            log.debug("Stopping telescope before Alt/Az movement")
            self._telescope.stop()
            await asyncio.sleep(0.5)  # Brief pause to ensure stop is processed

            # Convert Alt/Az to RA/Dec at current time
            time = AstropyTime.now()

            if self.observer is None:
                raise ValueError("No observer defined for coordinate conversion")

            # Create Alt/Az coordinate
            altaz_coord = SkyCoord(
                alt=alt * u.deg,
                az=az * u.deg,
                frame='altaz',
                obstime=time,
                location=self.observer
            )

            # Convert to ICRS (RA/Dec)
            icrs_coord = altaz_coord.transform_to('icrs')
            ra_deg = float(icrs_coord.ra.deg)
            dec_deg = float(icrs_coord.dec.deg)

            log.info(f"Converting Alt/Az ({alt:.1f}°, {az:.1f}°) to RA/Dec ({ra_deg:.3f}°, {dec_deg:.3f}°)")

            # Use the existing RA/Dec movement method
            await self._move_radec(ra_deg, dec_deg, abort_event)

        except Exception as e:
            log.error(f"Error during move to Alt/Az: {e}")
            raise exc.MoveError(f"Failed to move telescope to Alt/Az: {e}")

    async def _start_tracking(self) -> None:
        """Start sidereal tracking."""
        if not self._telescope:
            return

        try:
            if self._telescope.start_tracking(TrackingMode.SIDEREAL):
                log.info("Sidereal tracking started")
            else:
                log.warning("Failed to start sidereal tracking")
        except Exception as e:
            log.error(f"Error starting tracking: {e}")

    async def _auto_calibrate(self) -> None:
        """Perform auto-calibration using actual homed position to dynamically set limits."""
        if not self._telescope:
            raise exc.InitError("Telescope not connected for auto-calibration")

        try:
            # Get configuration
            auto_cal_config = self._calibration_data.get('auto_calibration', {})
            ranges = self._calibration_data.get('ranges', {})

            if not ranges:
                log.warning("No encoder ranges defined - skipping auto-calibration")
                return

            # Get nominal ranges from config
            ha_encoder_range = ranges.get('ha_encoder_range')
            dec_encoder_range = ranges.get('dec_encoder_range')

            if ha_encoder_range is None or dec_encoder_range is None:
                log.warning("Encoder ranges not defined - skipping auto-calibration")
                return

            log.info("Reading actual encoder positions after homing to establish new limits")

            # Wait a bit for telescope to fully settle after homing
            await asyncio.sleep(2.0)

            # Get actual encoder positions that were captured immediately after homing
            actual_ha_home, actual_dec_home = self._telescope.get_home_encoder_positions()

            if actual_ha_home is None or actual_dec_home is None:
                log.warning("Could not read captured home encoder positions - skipping auto-calibration")
                return

            # CRITICAL: Use the actual home encoder positions as the definitive limits
            # This replaces all configuration-based limits with measured reality

            log.info("=== ESTABLISHING DEFINITIVE TELESCOPE LIMITS FROM ACTUAL HOME POSITIONS ===")
            
            # The telescope homing process takes us to specific physical limit positions:
            # - HA axis: homes to the positive HA limit 
            # - Dec axis: homes to the negative Dec limit
            ha_positive_limit = actual_ha_home  # Physical positive HA limit (DEFINITIVE)
            dec_negative_limit = actual_dec_home  # Physical negative Dec limit (DEFINITIVE)
            
            # Calculate the opposite limits using the calibrated encoder ranges from config
            ha_negative_limit = actual_ha_home - ha_encoder_range
            dec_positive_limit = actual_dec_home + dec_encoder_range

            log.info(f"DEFINITIVE TELESCOPE LIMITS (from measured home positions + config ranges):")
            log.info(f"  HA positive limit: {ha_positive_limit} (MEASURED at home position)")
            log.info(f"  HA negative limit: {ha_negative_limit} (calculated: {actual_ha_home} - {ha_encoder_range})")
            log.info(f"  Dec negative limit: {dec_negative_limit} (MEASURED at home position)")
            log.info(f"  Dec positive limit: {dec_positive_limit} (calculated: {actual_dec_home} + {dec_encoder_range})")
            log.info(f"Using encoder ranges from config: HA={ha_encoder_range}, Dec={dec_encoder_range}")
            
            # WARNING: Check if Dec range seems reasonable
            dec_physical_range_degrees = dec_encoder_range / self._calibration_data.get('dec_steps_per_degree', 19408)
            log.info(f"Dec encoder range represents {dec_physical_range_degrees:.1f}° of physical motion")
            if dec_physical_range_degrees > 180:
                log.warning(f"Dec range of {dec_physical_range_degrees:.1f}° seems excessive - check config values")
            elif dec_physical_range_degrees < 90:
                log.warning(f"Dec range of {dec_physical_range_degrees:.1f}° seems too small for telescope operation")

            # Create the complete definitive limits dictionary
            definitive_limits = {
                'ha_positive': ha_positive_limit,
                'ha_negative': ha_negative_limit,
                'dec_positive': dec_positive_limit,
                'dec_negative': dec_negative_limit
            }

            # Update the telescope driver with these definitive limits
            success = self._telescope.update_calibration_limits(definitive_limits)
            if success:
                log.info("✓ Telescope driver updated with DEFINITIVE LIMITS from home positions")
                
                # Store ONLY the measured home positions in our calibration data
                # This ensures we always use measured reality, not configuration estimates
                if 'limits' not in self._calibration_data:
                    self._calibration_data['limits'] = {}
                    
                # Store the measured home positions as the authoritative limits
                self._calibration_data['limits']['ha_positive'] = ha_positive_limit
                self._calibration_data['limits']['dec_negative'] = dec_negative_limit
                
                # Store the calculated ranges for reference
                if 'measured_ranges' not in self._calibration_data:
                    self._calibration_data['measured_ranges'] = {}
                    
                self._calibration_data['measured_ranges']['ha_total_range'] = ha_encoder_range
                self._calibration_data['measured_ranges']['dec_total_range'] = dec_encoder_range
                
                # Update home position record
                if 'home_positions' not in self._calibration_data:
                    self._calibration_data['home_positions'] = {}
                    
                self._calibration_data['home_positions']['ha_encoder_at_home'] = actual_ha_home
                self._calibration_data['home_positions']['dec_encoder_at_home'] = actual_dec_home
                self._calibration_data['home_positions']['last_calibration_timestamp'] = Time.now().iso
                
                log.info("✓ Internal calibration data updated with measured home positions")
                log.info("=== TELESCOPE CALIBRATION COMPLETE - ALL LIMITS NOW BASED ON MEASURED REALITY ===")
                
            else:
                log.error("✗ Failed to update telescope driver with definitive limits")
                raise

        except Exception as e:
            log.error(f"Error during auto-calibration: {e}")
            raise

    async def _perform_initialization(self) -> None:
        """Perform complete telescope initialization (same as init() method but without timeout)."""
        if not self._telescope:
            raise exc.InitError("Telescope not connected")

        log.info("=== STARTING TELESCOPE INITIALIZATION SEQUENCE ===")
        log.info("Step 1: Setting initialization state and stopping all motion")
        await self._change_motion_status(MotionStatus.INITIALIZING)

        # Step 1: ALWAYS stop telescope before any movement command to prevent faults
        log.info("Stopping all telescope motion to ensure clean start")
        self._telescope.stop()
        await asyncio.sleep(1.0)  # Longer pause for initialization

        # Step 2: Home the telescope to get limit switch encoder positions
        log.info("Step 2: Homing telescope to establish encoder limit positions")
        if not self._telescope.home():
            raise exc.InitError("Failed to home telescope")

        # Step 3: Get the actual home encoder positions
        log.info("Step 3: Reading encoder positions at limit switches")
        actual_ha_home, actual_dec_home = self._telescope.get_home_encoder_positions()
        
        if actual_ha_home is None or actual_dec_home is None:
            log.warning("Could not read home encoder positions - auto-calibration will be limited")
        else:
            log.info(f"Home encoder positions captured: HA={actual_ha_home}, Dec={actual_dec_home}")

        # Step 4: Calculate and set definitive limits using ranges from config
        log.info("Step 4: Calculating telescope limits from home positions and config ranges")
        auto_cal_config = self._calibration_data.get('auto_calibration', {})
        if auto_cal_config.get('enabled', False) and auto_cal_config.get('calibrate_on_init', False):
            log.info("Performing DEFINITIVE telescope calibration from actual home positions")
            await self._auto_calibrate()
        else:
            log.warning("Auto-calibration is disabled - telescope limits will be based on configuration estimates only")
            log.warning("For optimal performance, enable auto-calibration to use measured encoder positions")

        await self._change_motion_status(MotionStatus.IDLE)
        log.info("=== TELESCOPE INITIALIZATION SEQUENCE COMPLETED SUCCESSFULLY ===")
        log.info("Telescope is ready for operation with definitive encoder limits")

    async def get_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """Get current RA/Dec coordinates.

        Returns:
            Tuple of (ra, dec) in degrees
        """
        if not self._telescope:
            raise exc.MotionError("Telescope not connected")

        try:
            # Get current HA/Dec from telescope
            ha, dec = self._telescope.current_position
            if ha is None or dec is None:
                raise exc.MotionError("Could not get current position")

            # Convert HA/Dec to RA/Dec
            time = AstropyTime.now()

            if self.observer is None:
                raise ValueError("No observer defined for coordinate conversion")

            # Calculate local sidereal time
            lst = time.sidereal_time('apparent', longitude=self.observer.lon)

            # Calculate RA (LST - HA)
            ra_deg = (lst.deg - ha * 15.0) % 360.0

            # Clamp declination to valid range to avoid Astropy validation errors
            dec_clamped = max(-89.999, min(89.999, dec))

            # Create coordinate for proper motion and precession correction
            coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_clamped * u.deg,
                           frame=FK5(equinox=time), obstime=time)
            icrs_coord = coord.transform_to('icrs')

            return float(icrs_coord.ra.deg), float(icrs_coord.dec.deg)

        except Exception as e:
            log.error(f"Error getting RA/Dec: {e}")
            raise exc.MotionError(f"Could not get current coordinates: {e}")

    async def get_altaz(self, **kwargs: Any) -> Tuple[float, float]:
        """Get current Alt/Az position.

        Returns:
            Tuple of (altitude, azimuth) in degrees
        """
        if not self._telescope:
            raise exc.MotionError("Telescope not connected")

        try:
            # Get current HA/Dec from telescope
            ha, dec = self._telescope.current_position
            if ha is None or dec is None:
                raise exc.MotionError("Could not get current position")

            # Convert HA/Dec to RA/Dec first
            time = AstropyTime.now()

            if self.observer is None:
                raise ValueError("No observer defined for coordinate conversion")

            # Calculate local sidereal time
            lst = time.sidereal_time('apparent', longitude=self.observer.lon)

            # Calculate RA (LST - HA)
            ra_deg = (lst.deg - ha * 15.0) % 360.0

            # Clamp declination to valid range to avoid Astropy validation errors
            dec_clamped = max(-89.999, min(89.999, dec))

            # Create RA/Dec coordinate
            radec_coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_clamped * u.deg,
                                 frame='icrs', obstime=time)

            # Convert to Alt/Az at current time and location
            from astropy.coordinates import AltAz
            altaz_frame = AltAz(obstime=time, location=self.observer)
            altaz_coord = radec_coord.transform_to(altaz_frame)

            return float(altaz_coord.alt.deg), float(altaz_coord.az.deg)

        except Exception as e:
            log.error(f"Error getting Alt/Az: {e}")
            raise exc.MotionError(f"Could not get current Alt/Az coordinates: {e}")

    @timeout(300)
    async def init(self, **kwargs: Any) -> None:
        """Initialize telescope by unparking and pointing to zenith."""
        if not self._telescope:
            raise exc.InitError("Telescope not connected")

        try:
            log.info("Initializing telescope (unpark and point to zenith)")
            await self._change_motion_status(MotionStatus.INITIALIZING)
            
            # Calculate zenith position for current time and location
            if self.observer is None:
                raise ValueError("No observer defined for zenith calculation")
                
            # Zenith is altitude = 90°, azimuth can be any value (use 0° = North)
            zenith_alt = 90.0
            zenith_az = 0.0
            
            log.info(f"Calculated zenith position: Alt={zenith_alt}°, Az={zenith_az}°")
            
            # Move to zenith position
            await self._move_altaz(zenith_alt, zenith_az, None)
            
            await self._change_motion_status(MotionStatus.IDLE)
            log.info("Telescope initialization completed successfully - pointing at zenith")

        except Exception as e:
            log.error(f"Error during initialization: {e}")
            await self._change_motion_status(MotionStatus.ERROR)
            raise exc.InitError(f"Could not initialize telescope: {e}")

    @timeout(600)  
    async def home_and_calibrate(self, **kwargs: Any) -> None:
        """Perform full telescope homing and calibration (same as auto-initialization)."""
        if not self._telescope:
            raise exc.InitError("Telescope not connected")

        try:
            log.info("Performing full telescope homing and calibration")
            await self._change_motion_status(MotionStatus.INITIALIZING)
            
            # Perform the complete initialization process
            await self._perform_initialization()
            
            await self._change_motion_status(MotionStatus.IDLE)
            log.info("Full homing and calibration completed successfully")

        except Exception as e:
            log.error(f"Error during homing and calibration: {e}")
            await self._change_motion_status(MotionStatus.ERROR)
            raise exc.InitError(f"Could not complete homing and calibration: {e}")

    @timeout(300)
    async def park(self, **kwargs: Any) -> None:
        """Park telescope."""
        if not self._telescope:
            raise exc.ParkError("Telescope not connected")

        try:
            log.info("Parking telescope")
            await self._change_motion_status(MotionStatus.PARKING)

            # ALWAYS stop telescope before any movement command to prevent faults
            log.debug("Stopping telescope before parking")
            self._telescope.stop()
            await asyncio.sleep(1.0)  # Longer pause for parking

            # Move to park position (SCP) using special park method
            park_ha = 0.0
            park_dec = -90.0  # Point to SCP

            if not self._telescope.park_to_ha_dec(park_ha, park_dec, SlewMode.NORMAL):
                raise exc.ParkError("Failed to initiate park slew")

            # Wait for completion - should go from PARKING to PARKED
            log.info("Waiting for parking to complete...")
            while self._telescope.status.state == MountState.PARKING:
                await asyncio.sleep(0.5)

            final_state = self._telescope.status.state
            log.info(f"Park wait loop exited - final state: {final_state}")

            if final_state == MountState.ERROR:
                raise exc.ParkError("Park slew failed")
            elif final_state == MountState.PARKED:
                log.info("✓ Telescope parked successfully")
            else:
                # Check if slew completed but didn't transition to PARKED properly
                if final_state == MountState.IDLE:
                    log.warning("Park slew completed but went to IDLE instead of PARKED - forcing PARKED state")
                    # Force the correct state since slew did complete
                    await self._change_motion_status(MotionStatus.PARKED)
                    log.info("✓ Telescope parking completed successfully")
                else:
                    log.error(f"✗ Unexpected park completion state: {final_state}")
                    raise exc.ParkError(f"Park failed with unexpected state: {final_state}")

        except Exception as e:
            log.error(f"Error during parking: {e}")
            await self._change_motion_status(MotionStatus.ERROR)
            raise exc.ParkError(f"Could not park telescope: {e}")

    async def stop_motion(self, device: Optional[str] = None, **kwargs: Any) -> None:
        """Stop telescope motion.

        Args:
            device: Device to stop (ignored, stops all motion)
        """
        if self._telescope:
            log.info("Stopping telescope motion")
            self._telescope.stop()

    async def is_ready(self, **kwargs: Any) -> bool:
        """Check if telescope is ready for operations.

        Returns:
            True if telescope is ready
        """
        if not self._telescope:
            return False

        state = self._telescope.status.state
        return state in [MountState.IDLE, MountState.TRACKING]


    async def set_offsets_radec(self, dra: float, ddec: float, **kwargs: Any) -> None:
        """Set RA/Dec offsets.

        Args:
            dra: RA offset in degrees.
            ddec: Dec offset in degrees.

        Raises:
            MoveError: If telescope cannot be moved.
        """
        log.info(f"Setting RA/Dec offsets: dRA={dra:.4f}°, dDec={ddec:.4f}°")

        # Store the new offsets
        self._offset_ra = dra
        self._offset_dec = ddec

        # If we have a target and telescope is tracking, apply the offset immediately
        if (self._target_ra is not None and self._target_dec is not None and
            self._telescope and self._telescope.is_tracking):

            log.info("Telescope is tracking - applying offset by moving to adjusted target position")

            try:
                # Calculate new target position with offsets
                ra_with_offset, dec_with_offset = self._apply_offsets_radec(self._target_ra, self._target_dec)

                # Convert to HA/Dec for telescope
                ha, dec_apparent = self._ra_dec_to_ha_dec(ra_with_offset, dec_with_offset)

                # ALWAYS stop telescope before any movement command to prevent faults
                log.debug("Stopping telescope before offset slew")
                self._telescope.stop()
                await asyncio.sleep(0.5)  # Brief pause to ensure stop is processed

                # Slew to new position with offset applied
                if self._telescope.slew_to_ha_dec(ha, dec_apparent, SlewMode.NORMAL):
                    log.info(f"Applied offset by slewing to new position: HA={ha:.3f}h, Dec={dec_apparent:.3f}°")
                else:
                    log.error("Failed to apply offset - slew to adjusted position failed")
                    raise exc.MoveError("Could not apply offset to telescope position")

            except Exception as e:
                log.error(f"Error applying offset to tracking telescope: {e}")
                raise exc.MoveError(f"Failed to apply offset: {e}")
        else:
            log.info("Offset stored - will be applied to next telescope movement")

    async def get_offsets_radec(self, **kwargs: Any) -> Tuple[float, float]:
        """Get RA/Dec offsets.

        Returns:
            Tuple with RA and Dec offsets in degrees.
        """
        return self._offset_ra, self._offset_dec


    def is_using_definitive_limits(self) -> bool:
        """Check if telescope is using definitive limits from measured home positions.
        
        Returns:
            True if limits are based on actual measured encoder positions
            False if using configuration-based estimates
        """
        if not self._calibration_data:
            return False
            
        home_positions = self._calibration_data.get('home_positions', {})
        return (
            'ha_encoder_at_home' in home_positions and
            'dec_encoder_at_home' in home_positions and
            'last_calibration_timestamp' in home_positions
        )
    
    def get_calibration_status(self) -> Dict[str, Any]:
        """Get detailed calibration status information.
        
        Returns:
            Dictionary containing calibration source and timestamp information
        """
        status = {
            'using_definitive_limits': self.is_using_definitive_limits(),
            'calibration_source': 'measured_encoder_positions' if self.is_using_definitive_limits() else 'configuration_estimates'
        }
        
        if self.is_using_definitive_limits():
            home_positions = self._calibration_data.get('home_positions', {})
            status.update({
                'last_calibration': home_positions.get('last_calibration_timestamp'),
                'measured_home_positions': {
                    'ha_encoder': home_positions.get('ha_encoder_at_home'),
                    'dec_encoder': home_positions.get('dec_encoder_at_home')
                }
            })
            
        return status

    @property
    def telescope_driver(self) -> Optional[TelescopeDriver]:
        """Access to underlying telescope driver for advanced operations."""
        return self._telescope


    async def _celestial(self) -> None:
        """Thread for continuously calculating positions and distances to celestial objects like moon and sun."""

        # wait a little
        await asyncio.sleep(10)

        # run until closing
        while True:
            # update headers
            try:
                await self._update_celestial_headers()
            except:
                log.exception("Something went wrong.")

            # sleep a little
            await asyncio.sleep(30)

    async def _update_celestial_headers(self) -> None:
        """Calculate positions and distances to celestial objects like moon and sun."""
        # get now as Astropy Time object
        now = AstropyTime.now()
        alt: Optional[float]
        az: Optional[float]

        # no observer?
        if self.observer is None:
            return

        # get telescope alt/az
        tel_altaz = None
        if isinstance(self, IPointingAltAz):
            try:
                alt, az = await self.get_altaz()
                tel_altaz = SkyCoord(
                    alt=alt * u.deg, az=az * u.deg, location=self.observer, obstime=now, frame="altaz"
                )
            except:
                log.exception("Could not fetch telescope Alt/Az: %s", self)
                return

        # get current moon and sun information using Astropy
        from astropy.coordinates import get_sun, get_moon
        from astropy.coordinates import AltAz

        # Get sun and moon positions
        sun_icrs = get_sun(now)
        moon_icrs = get_moon(now)

        # Convert to Alt/Az at observer location
        altaz_frame = AltAz(obstime=now, location=self.observer)
        sun_altaz = sun_icrs.transform_to(altaz_frame)
        moon_altaz = moon_icrs.transform_to(altaz_frame)

        # Calculate moon illumination fraction (simplified approximation)
        # This is a basic approximation - more sophisticated calculation would be needed for high precision
        moon_frac = 0.5  # Placeholder - proper calculation would require sun-moon-earth angles

        # store it
        self._celestial_headers = {
            "MOONALT": (float(moon_altaz.alt.deg), "Lunar altitude"),
            "MOONFRAC": (float(moon_frac), "Fraction of the moon illuminated"),
            "SUNALT": (float(sun_altaz.alt.deg), "Solar altitude"),
        }

        # calculate distance to telescope
        if tel_altaz is not None:
            moon_dist = tel_altaz.separation(moon_altaz) if tel_altaz is not None else None
            sun_dist = tel_altaz.separation(sun_altaz) if tel_altaz is not None else None
            self._celestial_headers["MOONDIST"] = (
                None if moon_dist is None else float(moon_dist.deg),
                "Lunar distance from target",
            )
            self._celestial_headers["SUNDIST"] = (
                None if sun_dist is None else float(sun_dist.deg),
                "Solar Distance from Target",
            )


__all__ = ["RotseTelescope"]