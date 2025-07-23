import asyncio
import logging
from datetime import datetime
from typing import Dict, Tuple, Optional, Callable
from enum import Enum
from dataclasses import dataclass
import yaml
import json

logger = logging.getLogger(__name__)


class CalibrationStatus(Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CalibrationPhase(Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    FINDING_RA_NEG = "finding_ra_negative"
    FINDING_RA_POS = "finding_ra_positive"
    FINDING_DEC_NEG = "finding_dec_negative"
    FINDING_DEC_POS = "finding_dec_positive"
    CALCULATING = "calculating"
    VALIDATING = "validating"
    COMPLETE = "complete"


@dataclass
class CalibrationConfig:
    """Configuration parameters for calibration."""
    motion_timeout: float = 420.0
    stability_threshold: int = 50  # encoder steps
    stability_checks: int = 5
    extreme_position: int = 15000000
    dec_total_range_degrees: float = 235.0  # 122° + 113°
    ra_total_range_hours: float = 12.0  # ±6 hours
    fast_velocity: int = 60000
    slow_velocity: int = 5000
    home_velocity: int = 65000
    positioning_velocity: int = 50000
    min_expected_range: int = 1000  # Minimum reasonable encoder range
    # New fields for config output
    sidereal_rate: int = 100  # encoder steps per second
    slew_speed: int = 5000  # max slew speed for telescope goto
    limits_safety_buffer: int = 100  # safety buffer around hardstops


@dataclass
class CalibrationProgress:
    status: CalibrationStatus
    phase: CalibrationPhase
    progress_percent: float
    current_operation: str
    error_message: Optional[str] = None
    limits_found: Dict[str, Optional[int]] = None


class CalibrationError(Exception):
    """Custom exception for calibration errors."""
    pass


class Calibration:
    """Calibration class for fork-mounted equatorial telescope."""

    def __init__(self, comm, config: Optional[CalibrationConfig] = None):
        """
        Initialize calibration module.

        Args:
            comm: Communication object with async methods:
                  get_encoder_positions(), move_ra(), move_dec(),
                  move_enc(), stop(), set_velocity()
            config: Calibration configuration parameters
        """
        self.mount = comm
        self.config = config or CalibrationConfig()

        self._calibration_data = {
            'limits': {
                'ra_negative': None,
                'ra_positive': None,
                'dec_negative': None,
                'dec_positive': None
            },
            'ranges': {
                'ra_encoder_range': None,
                'dec_encoder_range': None
            },
            'conversions': {
                'dec_degrees_per_step': None,
                'ra_hours_per_step': None,
            },
            'calibrated': False,
            'calibration_date': None,
            'config_used': self.config
        }

        self._status = CalibrationStatus.NOT_STARTED
        self._phase = CalibrationPhase.IDLE
        self._progress = 0.0
        self._current_operation = "Ready"
        self._error_message = None

        # Phase progress mapping for dynamic progress calculation
        self._phase_progress = {
            CalibrationPhase.IDLE: 0,
            CalibrationPhase.INITIALIZING: 5,
            CalibrationPhase.FINDING_RA_NEG: 15,
            CalibrationPhase.FINDING_RA_POS: 35,
            CalibrationPhase.FINDING_DEC_NEG: 55,
            CalibrationPhase.FINDING_DEC_POS: 75,
            CalibrationPhase.CALCULATING: 85,
            CalibrationPhase.VALIDATING: 95,
            CalibrationPhase.COMPLETE: 100
        }

    @property
    def is_calibrated(self) -> bool:
        """Check if telescope is calibrated."""
        return self._calibration_data['calibrated']

    @property
    def calibration_data(self) -> Dict:
        """Get copy of calibration data."""
        return self._calibration_data.copy()

    def get_progress(self) -> CalibrationProgress:
        """Get current calibration progress."""
        return CalibrationProgress(
            status=self._status,
            phase=self._phase,
            progress_percent=self._progress,
            current_operation=self._current_operation,
            error_message=self._error_message,
            limits_found=self._calibration_data['limits'].copy()
        )

    def _update_progress(self, phase: CalibrationPhase, operation: str,
                         progress_callback: Optional[Callable] = None):
        """Update progress and call callback if provided."""
        self._phase = phase
        self._progress = self._phase_progress.get(phase, 0)
        self._current_operation = operation

        logger.info(f"Calibration progress: {self._progress}% - {operation}")

        if progress_callback:
            progress_callback(self.get_progress())

    async def emergency_stop(self):
        """Emergency stop with error state."""
        logger.warning("Emergency stop triggered")
        await self.mount.stop()
        self._status = CalibrationStatus.FAILED
        self._error_message = "Emergency stop triggered"

    async def wait_for_motion_stop(self, axis: str, timeout: float = None) -> bool:
        """
        Wait for axis to stop moving by detecting position stability.

        Args:
            axis: 'RA' or 'DEC'
            timeout: Maximum time to wait (uses config default if None)

        Returns:
            True if motion stopped, False if timeout
        """
        if timeout is None:
            timeout = self.config.motion_timeout

        start_time = asyncio.get_event_loop().time()
        last_pos = None
        stable_count = 0

        logger.debug(f"Waiting for {axis} motion to stop (timeout: {timeout}s)")

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                ra_enc, dec_enc = await self.mount.get_encoder_positions()
                current_pos = ra_enc if axis.upper() == 'RA' else dec_enc

                if last_pos is not None:
                    movement = abs(current_pos - last_pos)
                    if movement < self.config.stability_threshold:
                        stable_count += 1
                        if stable_count >= self.config.stability_checks:
                            logger.debug(f"{axis} motion stopped at position {current_pos}")
                            return True
                    else:
                        stable_count = 0

                last_pos = current_pos
                await asyncio.sleep(1.0)

            except Exception as e:
                logger.error(f"Error checking motion status: {e}")
                return False

        logger.warning(f"Motion timeout for {axis} axis")
        return False

    async def find_limit(self, direction: str, axis: str) -> int:
        """
        Find limit switch position for given axis and direction.

        Args:
            direction: 'positive' or 'negative'
            axis: 'RA' or 'DEC'

        Returns:
            Encoder position at limit

        Raises:
            CalibrationError: If limit finding fails
        """
        logger.info(f"Finding {axis} {direction} limit")

        # Stop current motion
        await self.mount.stop()
        await asyncio.sleep(1)

        try:
            # Set appropriate speeds based on axis
            if axis.upper() == 'RA':
                await self.mount.set_velocity(self.config.fast_velocity, self.config.slow_velocity)
            else:
                await self.mount.set_velocity(self.config.slow_velocity, self.config.fast_velocity)

            # Move to extreme position to find limit
            extreme_pos = (self.config.extreme_position if direction == 'positive'
                           else -self.config.extreme_position)

            # Use appropriate move method
            if axis.upper() == 'RA':
                await self.mount.move_ra(extreme_pos)
            else:
                await self.mount.move_dec(extreme_pos)

            # Wait for motion to stop (should hit limit)
            if await self.wait_for_motion_stop(axis):
                ra_enc, dec_enc = await self.mount.get_encoder_positions()
                limit_pos = ra_enc if axis.upper() == 'RA' else dec_enc
                logger.info(f"{axis} {direction} limit found at position {limit_pos}")
                return limit_pos
            else:
                await self.mount.stop()
                raise CalibrationError(f"Failed to find {axis} {direction} limit - motion timeout")

        except Exception as e:
            await self.mount.stop()
            raise CalibrationError(f"Error finding {axis} {direction} limit: {e}")

    def _validate_limits(self):
        """Validate that found limits are reasonable."""
        logger.info("Validating calibration limits")

        limits = self._calibration_data['limits']

        # Check all limits were found
        for key, value in limits.items():
            if value is None:
                raise CalibrationError(f"Limit {key} not found")

        # Check ranges are reasonable
        ra_range = limits['ra_positive'] - limits['ra_negative']
        dec_range = limits['dec_positive'] - limits['dec_negative']

        if ra_range < self.config.min_expected_range:
            raise CalibrationError(f"RA range too small: {ra_range} steps")

        if dec_range < self.config.min_expected_range:
            raise CalibrationError(f"DEC range too small: {dec_range} steps")

        # Check that positive limits are actually greater than negative
        if limits['ra_positive'] <= limits['ra_negative']:
            raise CalibrationError("RA positive limit not greater than negative limit")

        if limits['dec_positive'] <= limits['dec_negative']:
            raise CalibrationError("DEC positive limit not greater than negative limit")

        logger.info(f"Limits validated - RA range: {ra_range}, DEC range: {dec_range}")

    async def calibrate(self, progress_callback: Optional[Callable[[CalibrationProgress], None]] = None) -> Dict:
        """
        Run complete calibration routine.

        Args:
            progress_callback: Optional callback function to receive progress updates

        Returns:
            Calibration data dictionary

        Raises:
            CalibrationError: If calibration fails
        """
        logger.info("Starting telescope calibration")

        try:
            self._status = CalibrationStatus.IN_PROGRESS
            self._error_message = None

            # Initialize
            self._update_progress(CalibrationPhase.INITIALIZING, "Initializing calibration", progress_callback)
            await self.mount.stop()
            await self.mount.set_velocity(self.config.fast_velocity, self.config.fast_velocity)
            await asyncio.sleep(2)

            # Find RA negative limit (west limit, -6 hours HA)
            self._update_progress(CalibrationPhase.FINDING_RA_NEG, "Finding RA negative limit", progress_callback)
            self._calibration_data['limits']['ra_negative'] = await self.find_limit('negative', 'RA')
            await asyncio.sleep(2)

            # Find RA positive limit (east limit, +6 hours HA)
            self._update_progress(CalibrationPhase.FINDING_RA_POS, "Finding RA positive limit", progress_callback)
            self._calibration_data['limits']['ra_positive'] = await self.find_limit('positive', 'RA')
            await asyncio.sleep(2)

            # Find Dec negative limit (southern limit)
            self._update_progress(CalibrationPhase.FINDING_DEC_NEG, "Finding DEC negative limit", progress_callback)
            self._calibration_data['limits']['dec_negative'] = await self.find_limit('negative', 'DEC')
            await asyncio.sleep(2)

            # Find Dec positive limit (northern limit)
            self._update_progress(CalibrationPhase.FINDING_DEC_POS, "Finding DEC positive limit", progress_callback)
            self._calibration_data['limits']['dec_positive'] = await self.find_limit('positive', 'DEC')

            # Calculate ranges and conversion factors
            self._update_progress(CalibrationPhase.CALCULATING, "Calculating ranges and conversions", progress_callback)

            ra_range = (self._calibration_data['limits']['ra_positive'] -
                        self._calibration_data['limits']['ra_negative'])
            dec_range = (self._calibration_data['limits']['dec_positive'] -
                         self._calibration_data['limits']['dec_negative'])

            self._calibration_data['ranges'] = {
                'ra_encoder_range': ra_range,
                'dec_encoder_range': dec_range
            }

            self._calibration_data['conversions'] = {
                'dec_degrees_per_step': self.config.dec_total_range_degrees / dec_range,
                'ra_hours_per_step': self.config.ra_total_range_hours / ra_range,
            }

            # Validate results
            self._update_progress(CalibrationPhase.VALIDATING, "Validating calibration results", progress_callback)
            self._validate_limits()

            # Mark as calibrated and complete
            self._calibration_data['calibrated'] = True
            self._calibration_data['calibration_date'] = datetime.now().isoformat()

            self._status = CalibrationStatus.COMPLETED
            self._update_progress(CalibrationPhase.COMPLETE, "Calibration completed successfully", progress_callback)

            logger.info("Telescope calibration completed successfully")
            return self._calibration_data.copy()

        except Exception as e:
            error_msg = f"Calibration failed: {e}"
            logger.error(error_msg)

            self._status = CalibrationStatus.FAILED
            self._error_message = str(e)
            self._current_operation = "Calibration failed"

            if progress_callback:
                progress_callback(self.get_progress())

            await self.mount.stop()
            raise CalibrationError(error_msg) from e

    def get_config_data(self) -> Dict:
        """Generate configuration data in the format you specified."""
        if not self.is_calibrated:
            raise CalibrationError("Cannot generate config data - telescope not calibrated")

        limits = self._calibration_data['limits']
        ranges = self._calibration_data['ranges']

        config_data = {
            # Overall calibration status
            'calibrated': True,

            # Date of calibration
            'calibration_date': self._calibration_data['calibration_date'],

            # Sidereal tracking speed in encoder steps / second
            'sidereal_rate': self.config.sidereal_rate,

            # Max slew speed for telescope goto
            'slew_speed': self.config.slew_speed,

            # Number of steps we define as a safety buffer around all encoder hardstops
            'limits_safety_buffer': self.config.limits_safety_buffer,

            # Encoder limits (raw encoder counts)
            'limits': {
                # RA axis encoder limits (±6 hours of movement)
                'ra_negative': limits['ra_negative'],  # Encoder count at HA = -6 hours
                'ra_positive': limits['ra_positive'],  # Encoder count at HA = +6 hours

                # Dec axis encoder limits
                'dec_negative': limits['dec_negative'],  # Encoder count at minimum declination
                'dec_positive': limits['dec_positive'],  # Encoder count at maximum declination
            },

            # Calculated ranges from limits
            'ranges': {
                'ra_encoder_range': ranges['ra_encoder_range'],  # ra_positive - ra_negative
                'dec_encoder_range': ranges['dec_encoder_range'],  # dec_positive - dec_negative
            },

            # Declination mechanical limits in degrees
            'dec_limits': {
                # HARDCODED DO NOT TOUCH: mount maps from +122 to -113 where 122 is the + pointing direction
                'positive_degrees': 122.0,  # Maximum mechanical declination
                'negative_degrees': -113.0,  # Minimum mechanical declination
                'dec_angular_range': 235.0,  # Total angular range (122 - (-113))
            },
        }

        return config_data

    def save_config_yaml(self, filepath: str):
        """Save calibration configuration to YAML file."""
        if not self.is_calibrated:
            raise CalibrationError("Cannot save config - telescope not calibrated")

        config_data = self.get_config_data()

        # Add comments to the YAML output
        yaml_content = f"""# Telescope Calibration Configuration
# Generated on: {datetime.now().isoformat()}
# 
# This file contains the calibration data for the telescope mount
# DO NOT EDIT MANUALLY unless you know what you're doing

# Overall calibration status
calibrated: {config_data['calibrated']}

# Date of calibration
calibration_date: '{config_data['calibration_date']}'

# Sidereal tracking speed in encoder steps / second
sidereal_rate: {config_data['sidereal_rate']}

# Max slew speed for telescope goto
slew_speed: {config_data['slew_speed']}

# Number of steps we define as a safety buffer around all encoder hardstops
limits_safety_buffer: {config_data['limits_safety_buffer']}

# Encoder limits (raw encoder counts)
limits:
  # RA axis encoder limits (±6 hours of movement)
  ra_negative: {config_data['limits']['ra_negative']}  # Encoder count at HA = -6 hours
  ra_positive: {config_data['limits']['ra_positive']}  # Encoder count at HA = +6 hours

  # Dec axis encoder limits
  dec_negative: {config_data['limits']['dec_negative']}  # Encoder count at minimum declination
  dec_positive: {config_data['limits']['dec_positive']}  # Encoder count at maximum declination

# Calculated ranges from limits
ranges:
  ra_encoder_range: {config_data['ranges']['ra_encoder_range']}  # ra_positive - ra_negative
  dec_encoder_range: {config_data['ranges']['dec_encoder_range']}  # dec_positive - dec_negative

# Declination mechanical limits in degrees
dec_limits:
  # HARDCODED DO NOT TOUCH: mount maps from +122 to -113 where 122 is the + pointing direction
  positive_degrees: 122.0  # Maximum mechanical declination
  negative_degrees: -113.0  # Minimum mechanical declination
  dec_angular_range: 235.0  # Total angular range (122 - (-113))
"""

        with open(filepath, 'w') as f:
            f.write(yaml_content)

        logger.info(f"Configuration saved to {filepath}")

    def load_config_yaml(self, filepath: str):
        """Load calibration configuration from YAML file."""
        try:
            with open(filepath, 'r') as f:
                config_data = yaml.safe_load(f)

            # Validate loaded data has required fields
            required_fields = ['calibrated', 'limits', 'ranges']
            for field in required_fields:
                if field not in config_data:
                    raise CalibrationError(f"Invalid config file: missing {field}")

            # Convert back to internal format
            self._calibration_data = {
                'limits': config_data['limits'],
                'ranges': config_data['ranges'],
                'conversions': {
                    'dec_degrees_per_step': self.config.dec_total_range_degrees / config_data['ranges'][
                        'dec_encoder_range'],
                    'ra_hours_per_step': self.config.ra_total_range_hours / config_data['ranges']['ra_encoder_range'],
                },
                'calibrated': config_data['calibrated'],
                'calibration_date': config_data.get('calibration_date'),
                'config_used': self.config
            }

            if config_data['calibrated']:
                self._status = CalibrationStatus.COMPLETED
                self._phase = CalibrationPhase.COMPLETE
                self._progress = 100.0
                self._current_operation = "Loaded from YAML file"

            logger.info(f"Configuration loaded from {filepath}")

        except FileNotFoundError:
            raise CalibrationError(f"Config file not found: {filepath}")
        except yaml.YAMLError:
            raise CalibrationError(f"Invalid YAML in config file: {filepath}")
        except Exception as e:
            raise CalibrationError(f"Error loading config file: {e}")

    def get_limits_summary(self) -> Dict[str, any]:
        """Get summary of calibration limits and ranges."""
        if not self.is_calibrated:
            return {"calibrated": False}

        limits = self._calibration_data['limits']
        ranges = self._calibration_data['ranges']
        conversions = self._calibration_data['conversions']

        # Calculate actual declination limits based on configuration
        dec_north_limit_degrees = 122.0  # From SCP
        dec_south_limit_degrees = -113.0  # From SCP

        return {
            "calibrated": True,
            "calibration_date": self._calibration_data['calibration_date'],
            "ra_range_steps": ranges['ra_encoder_range'],
            "dec_range_steps": ranges['dec_encoder_range'],
            "conversions": {
                "dec_degrees_per_step": conversions['dec_degrees_per_step'],
                "ra_hours_per_step": conversions['ra_hours_per_step']
            },
            "ra_limits": {
                "negative_hours": -6.0,  # West limit
                "positive_hours": 6.0,  # East limit
                "negative_encoder": limits['ra_negative'],
                "positive_encoder": limits['ra_positive']
            },
            "dec_limits": {
                "negative_degrees": dec_south_limit_degrees,  # Southern limit
                "positive_degrees": dec_north_limit_degrees,  # Northern limit
                "negative_encoder": limits['dec_negative'],
                "positive_encoder": limits['dec_positive'],
                "dec_angular_range": self.config.dec_total_range_degrees
            },
            "config_used": {
                "motion_timeout": self.config.motion_timeout,
                "stability_threshold": self.config.stability_threshold,
                "dec_total_range_degrees": self.config.dec_total_range_degrees,
                "ra_total_range_hours": self.config.ra_total_range_hours
            }
        }

    def reset_calibration(self):
        """Reset calibration data and status."""
        logger.info("Resetting calibration data")

        self._calibration_data = {
            'limits': {
                'ra_negative': None,
                'ra_positive': None,
                'dec_negative': None,
                'dec_positive': None
            },
            'ranges': {
                'ra_encoder_range': None,
                'dec_encoder_range': None
            },
            'conversions': {
                'dec_degrees_per_step': None,
                'ra_hours_per_step': None,
            },
            'calibrated': False,
            'calibration_date': None,
            'config_used': self.config
        }

        self._status = CalibrationStatus.NOT_STARTED
        self._phase = CalibrationPhase.IDLE
        self._progress = 0.0
        self._current_operation = "Ready"
        self._error_message = None

    def save_calibration(self, filepath: str):
        """Save calibration data to JSON file (legacy method)."""
        if not self.is_calibrated:
            raise CalibrationError("Cannot save uncalibrated data")

        # Convert dataclass to dict for JSON serialization
        data_to_save = self._calibration_data.copy()
        data_to_save['config_used'] = {
            'motion_timeout': self.config.motion_timeout,
            'stability_threshold': self.config.stability_threshold,
            'stability_checks': self.config.stability_checks,
            'extreme_position': self.config.extreme_position,
            'dec_total_range_degrees': self.config.dec_total_range_degrees,
            'ra_total_range_hours': self.config.ra_total_range_hours,
            'min_expected_range': self.config.min_expected_range
        }

        with open(filepath, 'w') as f:
            json.dump(data_to_save, f, indent=2)

        logger.info(f"Calibration data saved to {filepath}")

    def load_calibration(self, filepath: str):
        """Load calibration data from JSON file (legacy method)."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)

            # Validate loaded data has required fields
            required_fields = ['limits', 'ranges', 'conversions', 'calibrated']
            for field in required_fields:
                if field not in data:
                    raise CalibrationError(f"Invalid calibration file: missing {field}")

            self._calibration_data = data

            if data['calibrated']:
                self._status = CalibrationStatus.COMPLETED
                self._phase = CalibrationPhase.COMPLETE
                self._progress = 100.0
                self._current_operation = "Loaded from file"

            logger.info(f"Calibration data loaded from {filepath}")

        except FileNotFoundError:
            raise CalibrationError(f"Calibration file not found: {filepath}")
        except json.JSONDecodeError:
            raise CalibrationError(f"Invalid JSON in calibration file: {filepath}")
        except Exception as e:
            raise CalibrationError(f"Error loading calibration file: {e}")