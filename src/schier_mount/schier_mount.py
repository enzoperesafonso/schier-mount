import asyncio
from typing import Optional, Tuple, Dict, Any
from dataclasses import asdict
import logging

from .comm import Comm
from .coordinates import Coordinates
from .safety import Safety
from .state import MountState, MountStatus, TrackingMode, PierSide
from .tracking import Tracking

logger = logging.getLogger(__name__)


class SchierMount:
    """Main driver class for Schier telescope mount."""

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600, calibration_data: Optional[Dict] = None):
        """
        Initialize the mount driver.

        Args:
            device: Serial device path
            baudrate: Baud rate for communication
            calibration_data: Dictionary containing mount calibration parameters
        """
        self._comm = Comm(device, baudrate)
        self._status = MountStatus()
        self._calibration_data = calibration_data or {}

        # Initialize subsystems
        self._coordinates = Coordinates(self._status, self._calibration_data)
        self._safety = Safety(self._calibration_data)
        self._tracking = Tracking(self._status)

        # Movement parameters
        self._slew_rate = 1.0  # Default slew rate in deg/s
        self._tracking_rate = 15.041  # Sidereal rate in arcsec/s

    async def connect(self) -> None:
        """Initialize connection to the mount."""
        if self._status.state != MountState.DISCONNECTED:
            return

        self._status.state = MountState.INITIALIZING
        logger.info("Connecting to mount...")

        try:
            # Get initial position
            await self.update_position()
            self._status.state = MountState.IDLE
            logger.info("Mount connected successfully")
        except Exception as e:
            self._status.state = MountState.ERROR
            logger.error(f"Connection failed: {str(e)}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from the mount."""
        if self._status.state == MountState.DISCONNECTED:
            return

        logger.info("Disconnecting from mount...")
        await self.stop()
        self._status.state = MountState.DISCONNECTED

    async def home(self) -> None:
        """Home the mount (move to index positions)."""
        if not self._check_ready():
            return

        self._status.state = MountState.HOMING
        logger.info("Homing mount...")

        try:
            await self._comm.home()
            await self.update_position()
            self._status.state = MountState.IDLE
            logger.info("Mount homed successfully")
        except Exception as e:
            self._status.state = MountState.ERROR
            logger.error(f"Homing failed: {str(e)}")
            raise

    async def stop(self) -> None:
        """Stop all mount movement."""
        logger.info("Stopping mount movement")
        await self._comm.stop()
        self._status.is_moving = False
        await self._tracking.stop_track()
        await self.update_position()

        if self._status.state not in [MountState.PARKED, MountState.ERROR]:
            self._status.state = MountState.IDLE

    async def park(self) -> None:
        """Park the mount to a safe position."""
        if not self._check_ready():
            return

        self._status.state = MountState.PARKING
        logger.info("Parking mount...")

        try:
            # Move to home position (implementation specific)
            await self.home()
            self._status.state = MountState.PARKED
            logger.info("Mount parked successfully")
        except Exception as e:
            self._status.state = MountState.ERROR
            logger.error(f"Parking failed: {str(e)}")
            raise

    async def move_to_ha_dec(self, ha: float, dec: float) -> None:
        """
        Move mount to specified hour angle and declination.

        Args:
            ha: Hour angle in hours (-12 to +12)
            dec: Declination in degrees (-90 to +90)
        """
        if not self._check_ready():
            return

        logger.info(f"Moving to HA: {ha}h, Dec: {dec}°")
        self._status.state = MountState.SLEWING
        self._status.is_moving = True
        self._status.slew_start_time = asyncio.get_event_loop().time()

        # Convert coordinates to encoder positions
        ha_enc, dec_enc, below_pole = self._coordinates.ha_dec_to_encoder_positions(ha, dec)

        # Update pier side information
        self._status.pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL

        # Check safety limits
        if not self._safety.enc_position_is_within_safety_limits(ha_enc, dec_enc):
            logger.error("Target position exceeds safety limits")
            self._status.state = MountState.ERROR
            return

        # Update target position
        self._status.target_hour_angle = ha
        self._status.target_declination = dec
        self._status.target_ra_encoder = ha_enc
        self._status.target_dec_encoder = dec_enc

        try:
            # Move to position
            await self._comm.move_enc(ha_enc, dec_enc)

            # Wait until movement is complete (simplified)
            while self._status.is_moving:
                await self.update_position()
                await asyncio.sleep(0.1)

            self._status.state = MountState.IDLE
            logger.info("Movement completed successfully")
        except Exception as e:
            self._status.state = MountState.ERROR
            logger.error(f"Movement failed: {str(e)}")
            raise

    async def update_position(self) -> None:
        """Update the current position from the mount encoders."""
        try:
            ra_enc, dec_enc = await self._comm.get_encoder_positions()

            # Update encoder positions
            self._status.ra_encoder = ra_enc
            self._status.dec_encoder = dec_enc

            # Convert to HA/Dec
            ha, dec, below_pole = self._coordinates.encoder_positions_to_ha_dec(ra_enc, dec_enc)
            self._status.current_hour_angle = ha
            self._status.current_declination = dec

            # Update pier side information
            self._status.pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL

            # Check if we're still moving
            if (self._status.target_ra_encoder is not None and
                    self._status.target_dec_encoder is not None):
                pos_tolerance = 100  # encoder steps tolerance
                ra_diff = abs(ra_enc - self._status.target_ra_encoder)
                dec_diff = abs(dec_enc - self._status.target_dec_encoder)
                self._status.is_moving = ra_diff > pos_tolerance or dec_diff > pos_tolerance

            self._status.last_position_update = asyncio.get_event_loop().time()

        except Exception as e:
            logger.error(f"Position update failed: {str(e)}")
            raise

    async def track_sidereal(self) -> None:
        """Start sidereal tracking."""
        if not self._check_ready():
            return

        logger.info("Starting sidereal tracking")
        await self._tracking.track_sidereal()
        self._status.state = MountState.TRACKING

    async def track_non_sidereal(self, ha_rate: float, dec_rate: float) -> None:
        """
        Start non-sidereal tracking.

        Args:
            ha_rate: Hour angle tracking rate in arcsec/s
            dec_rate: Declination tracking rate in arcsec/s
        """
        if not self._check_ready():
            return

        logger.info(f"Starting non-sidereal tracking (HA: {ha_rate}, Dec: {dec_rate})")
        await self._tracking.track_non_sidereal(ha_rate, dec_rate)
        self._status.state = MountState.TRACKING

    async def stop_tracking(self) -> None:
        """Stop tracking."""
        logger.info("Stopping tracking")
        await self._tracking.stop_track()
        if self._status.state == MountState.TRACKING:
            self._status.state = MountState.IDLE

    def _check_ready(self) -> bool:
        """Check if mount is ready for operation."""
        if self._status.state == MountState.DISCONNECTED:
            logger.error("Mount is not connected")
            return False
        if self._status.state == MountState.ERROR:
            logger.error("Mount is in error state")
            return False
        if self._status.state == MountState.PARKED:
            logger.error("Mount is parked - unpark first")
            return False
        return True

    @property
    def status(self) -> Dict[str, Any]:
        """Return current mount status as a dictionary."""
        return asdict(self._status)

    @property
    def is_connected(self) -> bool:
        """Return True if mount is connected."""
        return self._status.state != MountState.DISCONNECTED

    @property
    def is_moving(self) -> bool:
        """Return True if mount is moving."""
        return self._status.is_moving

    @property
    def is_tracking(self) -> bool:
        """Return True if mount is tracking."""
        return self._status.tracking_mode != TrackingMode.STOPPED

    async def set_slew_rate(self, rate: float) -> None:
        """Set the slew rate in degrees per second."""
        self._slew_rate = rate
        # Convert to steps/s based on calibration data
        ra_vel = int(rate * self._calibration_data.get('ha_steps_per_degree', 1))
        dec_vel = int(rate * self._calibration_data.get('dec_steps_per_degree', 1))
        await self._comm.set_velocity(ra_vel, dec_vel)

    async def set_acceleration(self, acceleration: float) -> None:
        """Set the acceleration in degrees per second squared."""
        # Convert to steps/s² based on calibration data
        ra_acc = int(acceleration * self._calibration_data.get('ha_steps_per_degree', 1))
        dec_acc = int(acceleration * self._calibration_data.get('dec_steps_per_degree', 1))
        await self._comm.set_acceleration(ra_acc, dec_acc)