#!/usr/bin/env python3
"""
Test script for telescope calibration and pointing system.
This script simulates the mount hardware and tests the calibration and pointing logic.
"""

import asyncio
import random
from typing import Tuple, List
from datetime import datetime


# Mock hardware communication class
class MockComm:
    """Mock communication class that simulates telescope hardware"""

    def __init__(self):
        # Simulate encoder positions - start at some random position
        self.ra_encoder = random.randint(-1000000, 1000000)
        self.dec_encoder = random.randint(-1000000, 1000000)

        # Simulate physical limits (will be "discovered" during calibration)
        self.ra_limit_negative = -2500000  # West limit (-6 hours HA)
        self.ra_limit_positive = 2500000  # East limit (+6 hours HA)
        self.dec_limit_negative = -1800000  # Southern limit (113° from SCP)
        self.dec_limit_positive = 1900000  # Northern limit (122° from SCP)

        # Motion simulation
        self.ra_velocity = 0
        self.dec_velocity = 0
        self.ra_target = None
        self.dec_target = None
        self.moving = False

        print(f"Mock mount initialized:")
        print(f"  RA encoder: {self.ra_encoder}")
        print(f"  Dec encoder: {self.dec_encoder}")
        print(f"  RA limits: {self.ra_limit_negative} to {self.ra_limit_positive}")
        print(f"  Dec limits: {self.dec_limit_negative} to {self.dec_limit_positive}")

    async def get_encoder_positions(self) -> Tuple[int, int]:
        """Return current encoder positions"""
        # Simulate motion towards targets
        if self.moving and self.ra_target is not None:
            diff = self.ra_target - self.ra_encoder
            if abs(diff) > 100:
                self.ra_encoder += int(diff * 0.1)  # Move 10% closer
            else:
                self.ra_encoder = self.ra_target

        if self.moving and self.dec_target is not None:
            diff = self.dec_target - self.dec_encoder
            if abs(diff) > 100:
                self.dec_encoder += int(diff * 0.1)  # Move 10% closer
            else:
                self.dec_encoder = self.dec_target

        # Simulate hitting limits during calibration
        if self.ra_encoder < self.ra_limit_negative:
            self.ra_encoder = self.ra_limit_negative
            self.moving = False
        elif self.ra_encoder > self.ra_limit_positive:
            self.ra_encoder = self.ra_limit_positive
            self.moving = False

        if self.dec_encoder < self.dec_limit_negative:
            self.dec_encoder = self.dec_limit_negative
            self.moving = False
        elif self.dec_encoder > self.dec_limit_positive:
            self.dec_encoder = self.dec_limit_positive
            self.moving = False

        return self.ra_encoder, self.dec_encoder

    async def move_ra_enc(self, ra_enc: int) -> None:
        """Move RA to encoder position"""
        self.ra_target = ra_enc
        self.moving = True
        print(f"Moving RA to encoder {ra_enc}")

    async def move_dec_enc(self, dec_enc: int) -> None:
        """Move Dec to encoder position"""
        self.dec_target = dec_enc
        self.moving = True
        print(f"Moving Dec to encoder {dec_enc}")

    async def move_enc(self, ra_enc: int, dec_enc: int) -> None:
        """Move both axes to encoder positions"""
        self.ra_target = ra_enc
        self.dec_target = dec_enc
        self.moving = True
        print(f"Moving to RA={ra_enc}, Dec={dec_enc}")

    async def stop(self) -> None:
        """Stop all motion"""
        self.moving = False
        self.ra_target = None
        self.dec_target = None
        print("Motion stopped")

    async def set_velocity(self, ra_vel: int, dec_vel: int) -> None:
        """Set velocities"""
        self.ra_velocity = ra_vel
        self.dec_velocity = dec_vel
        print(f"Velocities set: RA={ra_vel}, Dec={dec_vel}")

    async def home(self) -> None:
        """Home the mount"""
        print("Homing mount...")
        await asyncio.sleep(2)


# Import our classes (assuming they're in separate files)
# For this test, we'll include simplified versions inline

from enum import Enum
from dataclasses import dataclass
from typing import Dict, Optional, Callable


class CalibrationStatus(Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CalibrationPhase(Enum):
    IDLE = "idle"
    FINDING_HA_NEG = "finding_ha_negative"
    FINDING_HA_POS = "finding_ha_positive"
    FINDING_DEC_NEG = "finding_dec_negative"
    FINDING_DEC_POS = "finding_dec_positive"
    COMPLETE = "complete"


@dataclass
class CalibrationProgress:
    status: CalibrationStatus
    phase: CalibrationPhase
    progress_percent: float
    current_operation: str
    error_message: Optional[str] = None
    limits_found: Dict[str, Optional[int]] = None


async def test_calibration():
    """Test the calibration process"""
    print("\n" + "=" * 60)
    print("TESTING CALIBRATION")
    print("=" * 60)

    # Create mock communication
    mock_comm = MockComm()

    # Import and create calibration instance
    # (In real usage, you'd import from your modules)
    from utils.calibration import Calibration
    calibration = Calibration(mock_comm, observatory_latitude=-22.9)

    # Progress callback to monitor calibration
    def progress_callback(progress: CalibrationProgress):
        print(f"Progress: {progress.progress_percent:5.1f}% - {progress.current_operation}")
        if progress.error_message:
            print(f"  ERROR: {progress.error_message}")
        if progress.limits_found:
            print(f"  Limits found: {progress.limits_found}")

    try:
        # Run calibration
        print("\nStarting calibration...")
        cal_data = await calibration.calibrate(progress_callback)

        print(f"\nCalibration completed successfully!")
        print(f"Calibration data: {cal_data}")

        # Test limits summary
        limits_summary = calibration.get_limits_summary()
        print(f"\nLimits summary:")
        for key, value in limits_summary.items():
            print(f"  {key}: {value}")

        return cal_data

    except Exception as e:
        print(f"Calibration failed: {e}")
        return None


async def test_pointing(calibration_data):
    """Test the pointing system"""
    print("\n" + "=" * 60)
    print("TESTING POINTING")
    print("=" * 60)

    if not calibration_data:
        print("Cannot test pointing - calibration failed")
        return

    # Create mock communication
    mock_comm = MockComm()

    # Import and create pointing instance
    # (In real usage, you'd import from your modules)
    from utils.pointing import SchierMountPointing
    pointing = SchierMountPointing(mock_comm, calibration_data, observatory_latitude=-22.9)

    # Test coordinate conversion
    print("\nTesting coordinate conversions...")

    test_coordinates = [
        (0.0, -45.0),  # Meridian, mid declination
        (-3.0, -90.0),  # West, south celestial pole
        (3.0, 0.0),  # East, celestial equator
        (-6.0, 30.0),  # West limit, north
        (6.0, -30.0),  # East limit, south
    ]

    for ha_hours, dec_degrees in test_coordinates:
        print(f"\nTesting HA={ha_hours:+5.1f}h, Dec={dec_degrees:+6.1f}°")

        try:
            # Test conversion to encoders and back
            from utils.pointing import SchierMountPointing
            test_pointing = SchierMountPointing(mock_comm, calibration_data, -22.9)

            ra_enc = test_pointing._ha_hours_to_encoder(ha_hours)
            dec_enc = test_pointing._dec_degrees_to_encoder(dec_degrees)
            print(f"  → Encoders: RA={ra_enc}, Dec={dec_enc}")

            ha_back = test_pointing._encoder_to_ha_hours(ra_enc)
            dec_back = test_pointing._encoder_to_dec_degrees(dec_enc)
            print(f"  → Back to coords: HA={ha_back:+5.1f}h, Dec={dec_back:+6.1f}°")

            ha_error = abs(ha_hours - ha_back)
            dec_error = abs(dec_degrees - dec_back)
            print(f"  → Errors: HA={ha_error:.3f}h, Dec={dec_error:.3f}°")

            if ha_error < 0.01 and dec_error < 0.1:
                print("  ✓ Conversion test PASSED")
            else:
                print("  ✗ Conversion test FAILED")

        except ValueError as e:
            print(f"  Expected error: {e}")

    # Test actual movements
    print(f"\nTesting actual pointing movements...")

    movement_tests = [
        (0.0, -45.0),  # Safe middle position
        (-2.0, 0.0),  # West side
        (2.0, -20.0),  # East side
    ]

    for ha_hours, dec_degrees in movement_tests:
        print(f"\nMoving to HA={ha_hours:+5.1f}h, Dec={dec_degrees:+6.1f}°")

        try:
            await pointing.goto_ha_dec(ha_hours, dec_degrees, wait_for_completion=True)

            # Wait for motion to settle
            await asyncio.sleep(2)

            # Check final position
            final_ha, final_dec = await pointing.get_ha_dec()
            print(f"  Final position: HA={final_ha:+5.1f}h, Dec={final_dec:+6.1f}°")

            ha_error = abs(ha_hours - final_ha)
            dec_error = abs(dec_degrees - final_dec)
            print(f"  Position errors: HA={ha_error:.3f}h, Dec={dec_error:.3f}°")

            if ha_error < 0.1 and dec_error < 1.0:
                print("  ✓ Movement test PASSED")
            else:
                print("  ✗ Movement test FAILED")

        except Exception as e:
            print(f"  Movement failed: {e}")

    # Test pointing info
    pointing_info = pointing.get_pointing_info()
    print(f"\nPointing system info:")
    for key, value in pointing_info.items():
        print(f"  {key}: {value}")


async def test_edge_cases():
    """Test edge cases and error conditions"""
    print("\n" + "=" * 60)
    print("TESTING EDGE CASES")
    print("=" * 60)

    # Test uncalibrated telescope
    print("\nTesting uncalibrated telescope...")
    mock_comm = MockComm()

    from utils.pointing import SchierMountPointing
    uncal_pointing = SchierMountPointing(mock_comm, {'calibrated': False}, -22.9)

    try:
        await uncal_pointing.goto_ha_dec(0.0, 0.0)
        print("  ✗ Should have failed for uncalibrated telescope")
    except ValueError as e:
        print(f"  ✓ Correctly rejected: {e}")

    # Test limits
    print(f"\nTesting coordinate limits...")
    mock_comm = MockComm()
    from utils.calibration import Calibration
    cal = Calibration(mock_comm)

    # Create fake calibration data for testing
    fake_cal_data = {
        'calibrated': True,
        'limits': {
            'ra_negative': -2500000,
            'ra_positive': 2500000,
            'dec_negative': -1800000,
            'dec_positive': 1900000
        },
        'ranges': {
            'ra_encoder_range': 5000000,
            'dec_encoder_range': 3700000
        }
    }

    pointing = SchierMountPointing(mock_comm, fake_cal_data, -22.9)

    # Test HA limits
    try:
        await pointing.goto_ha_dec(7.0, 0.0)  # Beyond +6h limit
        print("  ✗ Should have failed for HA > 6h")
    except ValueError as e:
        print(f"  ✓ Correctly rejected HA=7h: {e}")

    try:
        await pointing.goto_ha_dec(-7.0, 0.0)  # Beyond -6h limit
        print("  ✗ Should have failed for HA < -6h")
    except ValueError as e:
        print(f"  ✓ Correctly rejected HA=-7h: {e}")


async def run_all_tests():
    """Run all tests"""
    print("TELESCOPE CALIBRATION AND POINTING TEST SUITE")
    print("=" * 60)
    print(f"Test started at: {datetime.now()}")

    try:
        # Test calibration
        calibration_data = await test_calibration()

        # Test pointing (if calibration succeeded)
        if calibration_data:
            await test_pointing(calibration_data)

        # Test edge cases
        await test_edge_cases()

        print("\n" + "=" * 60)
        print("ALL TESTS COMPLETED")
        print("=" * 60)

    except Exception as e:
        print(f"\nTest suite failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # Run the test suite
    asyncio.run(run_all_tests())