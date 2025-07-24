import asyncio
import yaml
from datetime import datetime
from enum import Enum
from typing import Union, List, Tuple, Dict, Any
from pathlib import Path
import logging

# Assuming your existing modules are available
from comm import Comm


class CalibrationStep(Enum):
    SEARCH_NEGATIVE = 1
    SEARCH_POSITIVE = 2
    DONE = 3


class CalibrationAxis(Enum):
    RA = "ra"
    DEC = "dec"


class TelescopeCalibrator:
    """
    Auto calibration of rotse.

    This class handles the complete calibration process,
    including finding encoder limits and saving configuration data.
    """

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600,
                 config_file: str = "telescope_config.yaml"):
        self.comm = Comm(device, baudrate)
        self.config_file = Path(config_file)
        self.logger = self._setup_logger()

        # Calibration parameters
        self.search_velocity = 30000  # Slow speed for safety
        self.search_acceleration = 10000
        self.position_tolerance = 50  # Encoder counts tolerance for detecting limits
        self.status_check_interval = 0.5  # seconds
        self.movement_timeout = 600  # seconds max per axis movement

        # Safety buffer around limits
        self.limits_safety_buffer = 100

        # Hardcoded mechanical limits (as specified)
        self.dec_mechanical_limits = {
            'positive_degrees': 122.0,
            'negative_degrees': -113.0,
            'dec_angular_range': 235.0
        }

        self.ra_mechanical_limits = {
            'positive_hours': -6,
            'negative_hours': 6,
            'ra_angular_range': 12.0
        }

    def _setup_logger(self) -> logging.Logger:
        """Setup logging for calibration process."""
        logger = logging.getLogger('telescope_calibrator')
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

    async def _find_limit(self, axis: CalibrationAxis, direction: str) -> int:
        """Find a single limit for an axis with improved detection logic."""
        await self.comm.stop()

        if direction == "negative":
            target_position = -150000000
        else:
            target_position = 150000000

        if axis == CalibrationAxis.RA:
            await self.comm.move_ra_enc(target_position)
        else:
            await self.comm.move_dec_enc(target_position)

        # Enhanced monitoring with multiple criteria
        position_history = []
        stationary_count = 0
        start_time = asyncio.get_event_loop().time()
        last_significant_movement_time = start_time

        # Tunable parameters for more robust detection
        min_stationary_duration = 2.0  # Must be stationary for 2 seconds
        position_history_size = 10  # Track last 10 positions
        significant_movement_threshold = 200  # Larger threshold for "real" movement
        max_time_without_progress = 30  # Max time without significant movement

        while True:
            current_time = asyncio.get_event_loop().time()
            if current_time - start_time > self.movement_timeout:
                raise TimeoutError(f"Timeout while finding {axis.value} {direction} limit")

            ra_enc, dec_enc = await self.comm.get_encoder_positions()
            current_position = ra_enc if axis == CalibrationAxis.RA else dec_enc

            # Add current position to history
            position_history.append({
                'position': current_position,
                'timestamp': current_time
            })

            # Keep only recent history
            if len(position_history) > position_history_size:
                position_history.pop(0)

            if len(position_history) >= 2:
                # Check for immediate movement
                immediate_change = abs(current_position - position_history[-2]['position'])

                # Check for significant movement over longer period
                if len(position_history) >= position_history_size:
                    long_term_change = abs(current_position - position_history[0]['position'])
                    time_span = current_time - position_history[0]['timestamp']

                    if long_term_change > significant_movement_threshold:
                        # We've had significant movement, reset counters
                        last_significant_movement_time = current_time
                        stationary_count = 0
                    elif current_time - last_significant_movement_time > max_time_without_progress:
                        # No significant progress for too long, likely at limit
                        self.logger.warning(
                            f"No significant movement for {max_time_without_progress}s, assuming limit reached")
                        break

                # Short-term stationary detection (refined)
                if immediate_change < self.position_tolerance:
                    stationary_count += 1

                    # Calculate how long we've been stationary
                    stationary_duration = stationary_count * self.status_check_interval

                    if stationary_duration >= min_stationary_duration:
                        self.logger.info(f"{axis.value} {direction} limit found at: {current_position}")
                        break
                else:
                    stationary_count = 0

            await asyncio.sleep(self.status_check_interval)

        # Stop the axis and return final position
        await self.comm.stop()
        await asyncio.sleep(0.5)  # Allow time for stop command

        # Get final position after stopping
        ra_enc, dec_enc = await self.comm.get_encoder_positions()
        final_position = ra_enc if axis == CalibrationAxis.RA else dec_enc

        return final_position

    async def _move_away_from_limit(self, axis: CalibrationAxis, limit_position: int, direction: str):
        """Move away from a limit switch to avoid mechanical stress."""
        await self.comm.stop()

        if direction == "positive":
            safe_position = limit_position + (self.limits_safety_buffer * 2)
        else:
            safe_position = limit_position - (self.limits_safety_buffer * 2)

        self.logger.info(f"Moving {axis.value} away from limit to safe position: {safe_position}")

        if axis == CalibrationAxis.RA:
            await self.comm.move_ra_enc(safe_position)
        else:
            await self.comm.move_dec_enc(safe_position)

        # Wait for movement to complete
        await asyncio.sleep(10)
        await self.comm.stop()

    async def calibrate_mount(self) -> Dict[str, Any]:
        """
        Perform complete mount calibration.

        Returns:
            Dictionary containing all calibration data
        """
        self.logger.info("Starting telescope mount calibration")

        try:
            # Stop any current movement
            await self.comm.stop()
            await asyncio.sleep(1)

            # Find RA limits
            ra_negative, ra_positive = await self.find_axis_limits(CalibrationAxis.RA)

            # Find DEC limits
            dec_negative, dec_positive = await self.find_axis_limits(CalibrationAxis.DEC)

            # Calculate ranges
            ra_encoder_range = ra_positive - ra_negative
            dec_encoder_range = dec_positive - dec_negative

            # Create calibration data structure
            calibration_data = {
                'calibrated': True,
                'calibration_date': datetime.now().isoformat(),
                'sidereal_rate': 100,
                'slew_speed': 5000,
                'limits_safety_buffer': self.limits_safety_buffer,
                'limits': {
                    'ra_negative': ra_negative,
                    'ra_positive': ra_positive,
                    'dec_negative': dec_negative,
                    'dec_positive': dec_positive
                },
                'ranges': {
                    'ra_encoder_range': ra_encoder_range,
                    'dec_encoder_range': dec_encoder_range
                },
                'dec_limits': self.dec_mechanical_limits,
                'ra_limits': self.ra_mechanical_limits
            }

            self.logger.info("Calibration completed successfully")
            self.logger.info(f"RA range: {ra_negative} to {ra_positive} ({ra_encoder_range} steps)")
            self.logger.info(f"DEC range: {dec_negative} to {dec_positive} ({dec_encoder_range} steps)")

            return calibration_data

        except Exception as e:
            self.logger.error(f"Calibration failed: {str(e)}")
            # Ensure mount is stopped in case of error
            await self.comm.stop()
            raise

    def save_calibration_data(self, calibration_data: Dict[str, Any]) -> None:
        """Save calibration data to YAML file."""
        try:
            with open(self.config_file, 'w') as f:
                yaml.dump(calibration_data, f, default_flow_style=False, indent=2)
            self.logger.info(f"Calibration data saved to {self.config_file}")
        except Exception as e:
            self.logger.error(f"Failed to save calibration data: {str(e)}")
            raise

    def load_calibration_data(self) -> Dict[str, Any]:
        """Load calibration data from YAML file."""
        try:
            if not self.config_file.exists():
                raise FileNotFoundError(f"Configuration file {self.config_file} not found")

            with open(self.config_file, 'r') as f:
                data = yaml.safe_load(f)

            self.logger.info(f"Calibration data loaded from {self.config_file}")
            return data
        except Exception as e:
            self.logger.error(f"Failed to load calibration data: {str(e)}")
            raise

    async def run_full_calibration(self) -> Dict[str, Any]:
        """
        Run complete calibration process and save to file.

        Returns:
            The calibration data dictionary
        """
        self.logger.info("Starting full telescope calibration process")

        try:
            # Perform calibration
            calibration_data = await self.calibrate_mount()

            # Save to file
            self.save_calibration_data(calibration_data)

            self.logger.info("Full calibration process completed successfully")
            return calibration_data

        except Exception as e:
            self.logger.error(f"Full calibration process failed: {str(e)}")
            raise


# Convenience function for easy use
async def calibrate_telescope(device: str = "/dev/ttyS0",
                              baudrate: int = 9600,
                              config_file: str = "telescope_config.yaml") -> Dict[str, Any]:
    """
    Convenience function to run telescope calibration.

    Args:
        device: Serial device path
        baudrate: Serial communication baud rate
        config_file: Path to save configuration YAML file

    Returns:
        Dictionary containing calibration data
    """
    calibrator = TelescopeCalibrator(device, baudrate, config_file)
    return await calibrator.run_full_calibration()


# Example usage
if __name__ == "__main__":
    async def main():
        try:
            # Run calibration
            config = await calibrate_telescope(
                device="/dev/ttyS0",
                config_file="telescope_config.yaml"
            )
            print("Calibration completed successfully!")
            print(f"RA range: {config['ranges']['ra_encoder_range']} steps")
            print(f"DEC range: {config['ranges']['dec_encoder_range']} steps")

        except Exception as e:
            print(f"Calibration failed: {e}")


    # Run the calibration
    asyncio.run(main())