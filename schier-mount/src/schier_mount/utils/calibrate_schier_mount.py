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
        self.search_velocity = 40000  # Slow speed for safety
        self.search_acceleration = 20000
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

    async def find_axis_limits(self, axis: CalibrationAxis) -> Tuple[int, int]:
        """
        Find the encoder limits for a specific axis.

        Args:
            axis: The axis to calibrate (RA or DEC)

        Returns:
            Tuple of (negative_limit, positive_limit) encoder positions
        """
        self.logger.info(f"Starting {axis.value.upper()} axis limit detection")

        # Set safe velocity and acceleration
        if axis == CalibrationAxis.RA:
            await self.comm.set_velocity(self.search_velocity, 0)
            await self.comm.set_acceleration(self.search_acceleration, 0)
        else:
            await self.comm.set_velocity(0, self.search_velocity)
            await self.comm.set_acceleration(0, self.search_acceleration)

        # Find negative limit first
        self.logger.info(f"Searching for {axis.value} negative limit")
        negative_limit = await self._find_limit(axis, direction="negative")

        # Move away from negative limit
        await self._move_away_from_limit(axis, negative_limit, direction="positive")

        # Find positive limit
        self.logger.info(f"Searching for {axis.value} positive limit")
        positive_limit = await self._find_limit(axis, direction="positive")

        # Move away from positive limit
        await self._move_away_from_limit(axis, positive_limit, direction="negative")

        self.logger.info(f"{axis.value.upper()} limits found: negative={negative_limit}, positive={positive_limit}")

        return negative_limit, positive_limit

    async def _find_limit(self, axis: CalibrationAxis, direction: str) -> int:
        """Find a single limit for an axis."""
        # Move towards the limit
        if direction == "negative":
            target_position = -10000000  # Large negative number
        else:
            target_position = 10000000  # Large positive number

        if axis == CalibrationAxis.RA:
            await self.comm.move_ra_enc(target_position)
        else:
            await self.comm.move_dec_enc(target_position)

        # Monitor position until limit is reached
        last_position = None
        stationary_count = 0
        start_time = asyncio.get_event_loop().time()

        while True:
            current_time = asyncio.get_event_loop().time()
            if current_time - start_time > self.movement_timeout:
                raise TimeoutError(f"Timeout while finding {axis.value} {direction} limit")

            ra_enc, dec_enc = await self.comm.get_encoder_positions()
            current_position = ra_enc if axis == CalibrationAxis.RA else dec_enc

            if last_position is not None:
                position_change = abs(current_position - last_position)
                if position_change < self.position_tolerance:
                    stationary_count += 1
                    if stationary_count >= 3:  # Confirm we've hit the limit
                        # Stop the axis
                        await self.comm.stop()
                        self.logger.info(
                            f"{axis.value} {direction} limit found at encoder position: {current_position}")
                        return current_position
                else:
                    stationary_count = 0

            last_position = current_position
            await asyncio.sleep(self.status_check_interval)

    async def _move_away_from_limit(self, axis: CalibrationAxis, limit_position: int, direction: str):
        """Move away from a limit switch to avoid mechanical stress."""
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
        await asyncio.sleep(2)
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