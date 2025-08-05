import asyncio
import yaml
from datetime import datetime
from enum import Enum
from typing import Union, List, Tuple, Dict, Any
from pathlib import Path
import logging
from comm import Comm


class CalibrationStep(Enum):
    SEARCH_NEGATIVE = 1
    SEARCH_POSITIVE = 2
    DONE = 3


class CalibrationAxis(Enum):
    HA = "ha"  # Changed from RA to HA (Hour Angle)
    DEC = "dec"


class ROTSEMountStatus:
    """ROTSE III mount status bit definitions from Status2 command."""
    BRAKE_ENGAGED = 0x01  # b0: Brake is engaged
    AMPLIFIER_DISABLED = 0x02  # b1: Drive amplifier is disabled
    E_STOP_LIMIT = 0x04  # b2: In emergency-stop limit
    NEGATIVE_LIMIT = 0x08  # b3: In negative travel limit
    POSITIVE_LIMIT = 0x10  # b4: In positive travel limit


class TelescopeCalibrator:
    """
    Auto calibration of ROTSE III telescope mount with enhanced safety features.

    This class handles the complete calibration process for ROTSE III mounts,
    including proper emergency stop detection and limit switch monitoring.
    """

    def __init__(self, comm, config_file: str = "telescope_config.yaml"):
        self.comm = comm
        self.config_file = Path(config_file)
        self.logger = self._setup_logger()

        # ROTSE III specific calibration parameters
        self.initial_search_velocity = 10000  # Conservative speed for ROTSE III
        self.fine_search_velocity = 2000  # Very slow for final approach
        self.position_tolerance = 50  # Encoder counts tolerance
        self.status_check_interval = 0.2  # Check status every 200ms
        self.movement_timeout = 600  # seconds max per axis movement

        # Safety parameters
        self.limits_safety_buffer = 5000
        self.max_stationary_time = 2.0  # Reduced stationary detection time
        self.emergency_check_interval = 0.1  # Check emergency stop every 100ms

        # ROTSE III specific approach parameters
        self.approach_deceleration_distance = 20000  # Start slowing down this far from limit
        self.fine_approach_step = 500  # Small steps for final approach

        # Observatory configuration
        self.observer_latitude = -23.2716  # HESS Latitude

        # Mount specifications 
        self.ha_steps_per_degree = 3000
        self.dec_steps_per_degree = 19408
        self.sidereal_rate_ha_steps_per_sec = 100
        self.tracking_safety_buffer_steps = 2500
        self.limits_safety_factor = 0.05

    def _setup_logger(self) -> logging.Logger:
        """Setup logging for calibration process."""
        logger = logging.getLogger('rotse_calibrator')
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

    async def get_axis_status(self, axis: CalibrationAxis) -> int:
        """
        Get the status word for a specific axis using ROTSE III Status2 command.
        Returns the 16-bit status word as integer.
        """
        try:
            if axis == CalibrationAxis.HA:
                status_word = await self.comm.get_status2_ra()
            else:
                status_word = await self.comm.get_status2_dec()
            return status_word
        except Exception as e:
            self.logger.error(f"Failed to get {axis.value} status: {e}")
            # Return worst-case status if we can't read it
            return ROTSEMountStatus.E_STOP_LIMIT

    async def check_emergency_stop(self, axis: CalibrationAxis = None) -> bool:
        """
        Check if mount is in emergency stop condition using ROTSE III status bits.
        If axis is None, check both axes.
        """
        try:
            if axis is None:
                # Check both axes
                ra_status = await self.get_axis_status(CalibrationAxis.HA)
                dec_status = await self.get_axis_status(CalibrationAxis.DEC)

                ra_estop = (ra_status & ROTSEMountStatus.E_STOP_LIMIT) != 0
                dec_estop = (dec_status & ROTSEMountStatus.E_STOP_LIMIT) != 0

                return ra_estop or dec_estop
            else:
                # Check specific axis
                status = await self.get_axis_status(axis)
                return (status & ROTSEMountStatus.E_STOP_LIMIT) != 0

        except Exception as e:
            self.logger.warning(f"Could not check emergency stop status: {e}")
            return True  # Assume emergency stop if we can't check

    async def check_limit_switches(self, axis: CalibrationAxis) -> Tuple[bool, bool]:
        """
        Check limit switch status for an axis.
        Returns (negative_limit_active, positive_limit_active)
        """
        try:
            status = await self.get_axis_status(axis)
            negative_limit = (status & ROTSEMountStatus.NEGATIVE_LIMIT) != 0
            positive_limit = (status & ROTSEMountStatus.POSITIVE_LIMIT) != 0
            return negative_limit, positive_limit
        except Exception as e:
            self.logger.error(f"Failed to check limit switches for {axis.value}: {e}")
            return False, False

    async def is_axis_halted(self, axis: CalibrationAxis) -> bool:
        """Check if axis is in halted state (brake engaged, amplifier disabled)."""
        try:
            status = await self.get_axis_status(axis)
            brake_engaged = (status & ROTSEMountStatus.BRAKE_ENGAGED) != 0
            amp_disabled = (status & ROTSEMountStatus.AMPLIFIER_DISABLED) != 0
            return brake_engaged and amp_disabled
        except Exception as e:
            self.logger.error(f"Failed to check halt status for {axis.value}: {e}")
            return True  # Assume halted if we can't check

    async def clear_emergency_stop(self, axis: CalibrationAxis) -> bool:
        """
        Attempt to clear emergency stop condition for ROTSE III mount.
        According to the manual: "If there is an e-stop fault, the axis must be
        manually moved off of the e-stop limit switch."
        """
        try:
            self.logger.warning(f"Emergency stop detected on {axis.value} axis")
            self.logger.warning("ROTSE III requires manual intervention to clear e-stop")
            self.logger.warning("Please manually move the axis off the limit switch")

            # Check recent faults for more information
            try:
                faults = await self.comm.get_recent_faults()
                self.logger.info(f"Recent faults: {faults}")
            except Exception:
                pass

            # Wait for user intervention - check status periodically
            self.logger.info("Waiting for emergency stop to be cleared manually...")
            for i in range(60):  # Wait up to 60 seconds
                await asyncio.sleep(1)
                if not await self.check_emergency_stop(axis):
                    self.logger.info("Emergency stop cleared!")
                    return True
                if i % 10 == 0:
                    self.logger.info(f"Still waiting... ({60 - i} seconds remaining)")

            self.logger.error("Emergency stop not cleared within timeout")
            return False

        except Exception as e:
            self.logger.error(f"Failed to clear emergency stop: {e}")
            return False

    async def safe_stop_axis(self, axis: CalibrationAxis):
        """Safely stop an axis using ROTSE III StopRA/StopDec commands."""
        try:
            if axis == CalibrationAxis.HA:
                await self.comm.stop_ra()
            else:
                await self.comm.stop_dec()

            # Wait a moment for the stop to take effect
            await asyncio.sleep(0.5)

        except Exception as e:
            self.logger.error(f"Failed to stop {axis.value} axis: {e}")

    async def enable_axis_after_halt(self, axis: CalibrationAxis):
        """
        Enable axis after halt condition. ROTSE III requires Stop command first.
        """
        try:
            # Check if axis is halted
            if await self.is_axis_halted(axis):
                self.logger.info(f"Enabling {axis.value} axis after halt")
                # First command after halt must be Stop
                await self.safe_stop_axis(axis)
                await asyncio.sleep(1)

        except Exception as e:
            self.logger.error(f"Failed to enable {axis.value} axis: {e}")
            raise

    async def find_axis_limits(self, axis: CalibrationAxis) -> Tuple[int, int]:
        """
        Find the encoder limits for a specific axis using ROTSE III safe approach.
        """
        self.logger.info(f"Starting {axis.value.upper()} axis limit detection")

        # Check for emergency stop before starting
        if await self.check_emergency_stop(axis):
            if not await self.clear_emergency_stop(axis):
                raise RuntimeError(f"{axis.value} axis is in emergency stop condition. Manual intervention required.")

        # Ensure axis is enabled
        await self.enable_axis_after_halt(axis)

        # Set conservative parameters for limit finding
        if axis == CalibrationAxis.HA:
            await self.comm.set_accel_ra(5000)  # Conservative acceleration
            await self.comm.set_max_vel_ra(self.initial_search_velocity)
        else:
            await self.comm.set_accel_dec(5000)
            await self.comm.set_max_vel_dec(self.initial_search_velocity)

        # Find negative limit first
        self.logger.info(f"Searching for {axis.value} negative limit")
        negative_limit = await self._find_limit_with_switches(axis, direction="negative")

        # Move away from negative limit
        await self._move_away_from_limit(axis, negative_limit, direction="positive")

        # Find positive limit
        self.logger.info(f"Searching for {axis.value} positive limit")
        positive_limit = await self._find_limit_with_switches(axis, direction="positive")

        # Move away from positive limit
        await self._move_away_from_limit(axis, positive_limit, direction="negative")

        self.logger.info(f"{axis.value.upper()} limits found: negative={negative_limit}, positive={positive_limit}")
        return negative_limit, positive_limit

    async def _find_limit_with_switches(self, axis: CalibrationAxis, direction: str) -> int:
        """
        Find limit using ROTSE III limit switches for immediate detection.
        """
        await self.safe_stop_axis(axis)
        await asyncio.sleep(1)

        # Set target position far in the desired direction
        if direction == "negative":
            target_position = -500000  # Large negative number
        else:
            target_position = 500000  # Large positive number

        # Start moving towards limit
        if axis == CalibrationAxis.HA:
            await self.comm.move_ra_enc(target_position)
        else:
            await self.comm.move_dec_enc(target_position)

        self.logger.info(f"Moving {axis.value} towards {direction} limit")

        start_time = asyncio.get_event_loop().time()
        last_position = None
        stationary_count = 0

        while True:
            current_time = asyncio.get_event_loop().time()
            if current_time - start_time > self.movement_timeout:
                await self.safe_stop_axis(axis)
                raise TimeoutError(f"Timeout while finding {axis.value} {direction} limit")

            # Check for emergency stop
            if await self.check_emergency_stop(axis):
                await self.safe_stop_axis(axis)
                raise RuntimeError(f"Emergency stop triggered while finding {axis.value} {direction} limit")

            # Check limit switches - this is the key safety feature
            neg_limit, pos_limit = await self.check_limit_switches(axis)

            if direction == "negative" and neg_limit:
                await self.safe_stop_axis(axis)
                ra_enc, dec_enc = await self.comm.get_encoder_positions()
                current_position = ra_enc if axis == CalibrationAxis.HA else dec_enc
                self.logger.info(f"{axis.value} negative limit switch activated at position {current_position}")
                return current_position

            elif direction == "positive" and pos_limit:
                await self.safe_stop_axis(axis)
                ra_enc, dec_enc = await self.comm.get_encoder_positions()
                current_position = ra_enc if axis == CalibrationAxis.HA else dec_enc
                self.logger.info(f"{axis.value} positive limit switch activated at position {current_position}")
                return current_position

            # Also check for stationary condition as backup
            ra_enc, dec_enc = await self.comm.get_encoder_positions()
            current_position = ra_enc if axis == CalibrationAxis.HA else dec_enc

            if last_position is not None:
                position_change = abs(current_position - last_position)
                if position_change < self.position_tolerance:
                    stationary_count += 1
                    if stationary_count >= int(self.max_stationary_time / self.status_check_interval):
                        # Been stationary too long - likely hit mechanical limit
                        await self.safe_stop_axis(axis)
                        self.logger.warning(
                            f"{axis.value} axis appears to have hit mechanical limit at {current_position}")
                        self.logger.warning("This may indicate a problem with limit switches")
                        return current_position
                else:
                    stationary_count = 0

            last_position = current_position
            await asyncio.sleep(self.status_check_interval)

    async def _move_away_from_limit(self, axis: CalibrationAxis, limit_position: int, direction: str):
        """Move away from a limit switch to avoid mechanical stress."""
        await self.safe_stop_axis(axis)
        await asyncio.sleep(1)

        if direction == "positive":
            safe_position = limit_position + (self.limits_safety_buffer * 2)
        else:
            safe_position = limit_position - (self.limits_safety_buffer * 2)

        self.logger.info(f"Moving {axis.value} away from limit to safe position: {safe_position}")

        # Use slower speed for safety moves
        if axis == CalibrationAxis.HA:
            await self.comm.set_max_vel_ra(self.fine_search_velocity)
            await self.comm.move_ra_enc(safe_position)
        else:
            await self.comm.set_max_vel_dec(self.fine_search_velocity)
            await self.comm.move_dec_enc(safe_position)

        # Monitor the safety move
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < 30:  # 30 second timeout

            # Check for emergency conditions
            if await self.check_emergency_stop(axis):
                await self.safe_stop_axis(axis)
                raise RuntimeError(f"Emergency stop during {axis.value} safety move")

            # Check if we've reached the safe position
            ra_enc, dec_enc = await self.comm.get_encoder_positions()
            current_pos = ra_enc if axis == CalibrationAxis.HA else dec_enc

            if abs(current_pos - safe_position) < self.position_tolerance * 2:
                self.logger.info(f"{axis.value} safely moved to position {current_pos}")
                break

            await asyncio.sleep(0.5)

        await self.safe_stop_axis(axis)

    async def calibrate_mount(self) -> Dict[str, Any]:
        """
        Perform complete mount calibration with ROTSE III safety features.
        """
        self.logger.info("Starting ROTSE III telescope mount calibration")

        try:
            # Check initial emergency stop condition
            if await self.check_emergency_stop():
                self.logger.error("Mount is in emergency stop condition")
                # Try to get fault information
                try:
                    faults = await self.comm.get_recent_faults()
                    self.logger.error(f"Recent faults: {faults}")
                except Exception:
                    pass
                raise RuntimeError("Cannot start calibration - emergency stop condition detected")

            # Stop any current movement and prepare both axes
            await self.safe_stop_axis(CalibrationAxis.HA)
            await self.safe_stop_axis(CalibrationAxis.DEC)
            await asyncio.sleep(2)

            # Enable axes if they were halted
            await self.enable_axis_after_halt(CalibrationAxis.HA)
            await self.enable_axis_after_halt(CalibrationAxis.DEC)

            # Find HA limits (using RA motor)
            ha_negative, ha_positive = await self.find_axis_limits(CalibrationAxis.HA)

            # Find DEC limits
            dec_negative, dec_positive = await self.find_axis_limits(CalibrationAxis.DEC)

            # Calculate ranges
            ha_encoder_range = ha_positive - ha_negative
            dec_encoder_range = dec_positive - dec_negative

            # Create calibration data structure
            calibration_data = {
                'calibrated': True,
                'calibration_date': datetime.now().isoformat(),
                'mount_type': 'ROTSE_III',
                'observer_latitude': self.observer_latitude,
                'limits': {
                    'ha_negative': ha_negative,
                    'ha_positive': ha_positive,
                    'dec_negative': dec_negative,
                    'dec_positive': dec_positive
                },
                'ranges': {
                    'ha_encoder_range': ha_encoder_range,
                    'dec_encoder_range': dec_encoder_range
                },
                'ha_steps_per_degree': ha_encoder_range / 180,
                'dec_steps_per_degree': self.dec_steps_per_degree,
                'sidereal_rate_ha_steps_per_sec': (ha_encoder_range * 2) / 86164.0905,
                'tracking_safety_buffer_steps': self.tracking_safety_buffer_steps,
                'limits_safety_factor': self.limits_safety_factor,
                'calibration_velocities': {
                    'search_velocity': self.initial_search_velocity,
                    'fine_velocity': self.fine_search_velocity
                }
            }

            # Final safety stop
            await self.safe_stop_axis(CalibrationAxis.HA)
            await self.safe_stop_axis(CalibrationAxis.DEC)

            self.logger.info("ROTSE III calibration completed successfully")
            self.logger.info(f"HA range: {ha_negative} to {ha_positive} ({ha_encoder_range} steps)")
            self.logger.info(f"DEC range: {dec_negative} to {dec_positive} ({dec_encoder_range} steps)")

            return calibration_data

        except Exception as e:
            self.logger.error(f"ROTSE III calibration failed: {str(e)}")
            # Ensure mount is stopped in case of error
            try:
                await self.safe_stop_axis(CalibrationAxis.HA)
                await self.safe_stop_axis(CalibrationAxis.DEC)
            except Exception:
                pass
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
        Run complete ROTSE III calibration process and save to file.
        """
        self.logger.info("Starting full ROTSE III telescope calibration process")

        try:
            # Check that we can communicate with the mount
            try:
                await self.get_axis_status(CalibrationAxis.HA)
                await self.get_axis_status(CalibrationAxis.DEC)
            except Exception as e:
                raise RuntimeError(f"Cannot communicate with ROTSE III mount: {e}")

            # Perform calibration
            calibration_data = await self.calibrate_mount()

            # Save to file
            self.save_calibration_data(calibration_data)

            self.logger.info("Full ROTSE III calibration process completed successfully")
            return calibration_data

        except Exception as e:
            self.logger.error(f"Full ROTSE III calibration process failed: {str(e)}")
            raise


async def calibrate_telescope(device: str = "/dev/ttyS0",
                              baudrate: int = 9600,
                              config_file: str = "telescope_config.yaml") -> Dict[str, Any]:
    """
    Convenience function to run ROTSE III telescope calibration.
    """
    comm = Comm(device=device, baudrate=baudrate)
    calibrator = TelescopeCalibrator(comm, config_file=config_file)
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
            print("ROTSE III Calibration completed successfully!")
            print(f"HA range: {config['ranges']['ha_encoder_range']} steps")
            print(f"DEC range: {config['ranges']['dec_encoder_range']} steps")
            print(f"Mount type: {config['mount_type']}")

        except Exception as e:
            print(f"ROTSE III Calibration failed: {e}")


    # Run the calibration
    asyncio.run(main())