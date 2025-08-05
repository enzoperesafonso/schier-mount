#!/usr/bin/env python3
"""
ROTSE III Telescope Mount Calibration Script

This script performs a complete, safe calibration of a ROTSE III telescope mount.
It will automatically find the encoder limits for both axes and generate a
configuration file for telescope pointing.

Usage:
    python calibration.py [options]

The script will:
1. Connect to the mount
2. Safely find HA and DEC axis limits using hardware limit switches
3. Calculate calibration parameters
4. Save everything to telescope_config.yaml

Press Ctrl+C at any time to safely stop the calibration.
"""

import asyncio
import yaml
import sys
import argparse
from datetime import datetime
from enum import Enum
from typing import Union, List, Tuple, Dict, Any
from pathlib import Path
import logging
import signal

# Import your existing communication module
try:
    from comm import Comm

    COMM_AVAILABLE = True
except ImportError:
    print("❌ ERROR: comm.py not found!")
    print("   Please ensure comm.py is in the same directory as this script.")
    sys.exit(1)


class CalibrationAxis(Enum):
    HA = "ha"
    DEC = "dec"


class ROTSEMountStatus:
    """ROTSE III mount status bit definitions from Status2 command."""
    BRAKE_ENGAGED = 0x01
    AMPLIFIER_DISABLED = 0x02
    E_STOP_LIMIT = 0x04
    NEGATIVE_LIMIT = 0x08
    POSITIVE_LIMIT = 0x10


class EnhancedComm(Comm):
    """
    Enhanced communication class that adds ROTSE III specific methods
    to your existing Comm class.
    """

    async def get_status2_ra(self) -> int:
        """Get RA axis status word (16-bit hex)"""
        response = await self.send_commands("$Status2RA")
        try:
            # Response format: "@Status2RA HHHH" where HHHH is hex status
            if response and len(response) >= 4:
                # Extract the hex part and convert to int
                hex_status = response.strip()
                return int(hex_status, 16)
            return 0
        except (ValueError, IndexError) as e:
            self.logger.error(f"Failed to parse RA status response '{response}': {e}")
            return ROTSEMountStatus.E_STOP_LIMIT  # Assume worst case

    async def get_status2_dec(self) -> int:
        """Get DEC axis status word (16-bit hex)"""
        response = await self.send_commands("$Status2Dec")
        try:
            if response and len(response) >= 4:
                hex_status = response.strip()
                return int(hex_status, 16)
            return 0
        except (ValueError, IndexError) as e:
            self.logger.error(f"Failed to parse DEC status response '{response}': {e}")
            return ROTSEMountStatus.E_STOP_LIMIT

    async def stop_ra(self):
        """Stop RA axis with controlled deceleration"""
        await self.send_commands("$StopRA")

    async def stop_dec(self):
        """Stop DEC axis with controlled deceleration"""
        await self.send_commands("$StopDec")

    async def set_accel_ra(self, accel: int):
        """Set RA acceleration in counts/sec^2"""
        await self.send_commands(f"$AccelRa {accel}")

    async def set_accel_dec(self, accel: int):
        """Set DEC acceleration in counts/sec^2"""
        await self.send_commands(f"$AccelDec {accel}")

    async def set_max_vel_ra(self, vel: int):
        """Set RA maximum velocity in counts/sec"""
        await self.send_commands(f"$MaxVelRA {vel}")

    async def set_max_vel_dec(self, vel: int):
        """Set DEC maximum velocity in counts/sec"""
        await self.send_commands(f"$MaxVelDec {vel}")

    async def get_recent_faults(self) -> str:
        """Get recent fault list"""
        try:
            response = await self.send_commands("$RecentFaults")
            return response if response else "No faults reported"
        except Exception as e:
            return f"Failed to get faults: {e}"

    # Override move methods to work without $RunRA/$RunDec which aren't in ROTSE III spec
    async def move_ra_enc_direct(self, ra_enc: int) -> None:
        """Move RA to encoder position using direct ROTSE III command"""
        await self.send_commands(f"$PosRA {ra_enc}")

    async def move_dec_enc_direct(self, dec_enc: int) -> None:
        """Move DEC to encoder position using direct ROTSE III command"""
        await self.send_commands(f"$PosDec {dec_enc}")


class TelescopeCalibrator:
    """Complete ROTSE III telescope calibration system."""

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600,
                 config_file: str = "telescope_config.yaml",
                 latitude: float = -23.2716):

        # Use enhanced communication class
        self.comm = EnhancedComm(device=device, baudrate=baudrate)
        self.comm.logger = self._setup_logger()  # Add logger to comm for error handling

        self.config_file = Path(config_file)
        self.observer_latitude = latitude
        self.logger = self.comm.logger

        # Calibration parameters - conservative for safety
        self.initial_search_velocity = 8000  # Conservative initial speed
        self.fine_search_velocity = 1500  # Very slow final approach
        self.position_tolerance = 50
        self.status_check_interval = 0.2
        self.movement_timeout = 600
        self.limits_safety_buffer = 5000
        self.max_stationary_time = 3.0

        # Mount specifications (adjust as needed)
        self.dec_steps_per_degree = 19408
        self.tracking_safety_buffer_steps = 2500
        self.limits_safety_factor = 0.05

        # Graceful shutdown handling
        self.shutdown_requested = False
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle Ctrl+C gracefully."""
        print(f"\n🛑 Shutdown requested (signal {signum})")
        self.shutdown_requested = True

    def _setup_logger(self) -> logging.Logger:
        """Setup comprehensive logging."""
        logger = logging.getLogger('rotse_calibrator')
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            # Console handler
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%H:%M:%S'
            )
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)

            # File handler
            file_handler = logging.FileHandler('calibration.log')
            file_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)

        return logger

    async def check_shutdown(self):
        """Check if shutdown was requested."""
        if self.shutdown_requested:
            self.logger.info("Shutdown requested - stopping all motion")
            try:
                await self.comm.stop_ra()
                await self.comm.stop_dec()
            except Exception:
                pass
            raise KeyboardInterrupt("Calibration stopped by user")

    async def get_axis_status(self, axis: CalibrationAxis) -> int:
        """Get status word for axis."""
        try:
            if axis == CalibrationAxis.HA:
                return await self.comm.get_status2_ra()
            else:
                return await self.comm.get_status2_dec()
        except Exception as e:
            self.logger.error(f"Failed to get {axis.value} status: {e}")
            return ROTSEMountStatus.E_STOP_LIMIT

    async def check_emergency_stop(self, axis: CalibrationAxis = None) -> bool:
        """Check for emergency stop condition."""
        try:
            if axis is None:
                ra_status = await self.get_axis_status(CalibrationAxis.HA)
                dec_status = await self.get_axis_status(CalibrationAxis.DEC)
                ra_estop = (ra_status & ROTSEMountStatus.E_STOP_LIMIT) != 0
                dec_estop = (dec_status & ROTSEMountStatus.E_STOP_LIMIT) != 0
                return ra_estop or dec_estop
            else:
                status = await self.get_axis_status(axis)
                return (status & ROTSEMountStatus.E_STOP_LIMIT) != 0
        except Exception as e:
            self.logger.warning(f"Could not check emergency stop: {e}")
            return True

    async def check_limit_switches(self, axis: CalibrationAxis) -> Tuple[bool, bool]:
        """Check limit switches. Returns (negative_active, positive_active)."""
        try:
            status = await self.get_axis_status(axis)
            negative = (status & ROTSEMountStatus.NEGATIVE_LIMIT) != 0
            positive = (status & ROTSEMountStatus.POSITIVE_LIMIT) != 0
            return negative, positive
        except Exception as e:
            self.logger.error(f"Failed to check limits for {axis.value}: {e}")
            return False, False

    async def safe_stop_axis(self, axis: CalibrationAxis):
        """Safely stop an axis."""
        try:
            if axis == CalibrationAxis.HA:
                await self.comm.stop_ra()
            else:
                await self.comm.stop_dec()
            await asyncio.sleep(0.5)
        except Exception as e:
            self.logger.error(f"Failed to stop {axis.value}: {e}")

    async def prepare_axis(self, axis: CalibrationAxis):
        """Prepare axis for calibration."""
        self.logger.info(f"Preparing {axis.value.upper()} axis...")

        # Stop and set parameters
        await self.safe_stop_axis(axis)
        await asyncio.sleep(1)

        if axis == CalibrationAxis.HA:
            await self.comm.set_accel_ra(5000)
            await self.comm.set_max_vel_ra(self.initial_search_velocity)
        else:
            await self.comm.set_accel_dec(5000)
            await self.comm.set_max_vel_dec(self.initial_search_velocity)

    async def test_communication(self) -> bool:
        """Test communication with mount."""
        try:
            self.logger.info("Testing communication with ROTSE III mount...")

            # Try to get current positions
            ra_pos, dec_pos = await self.comm.get_encoder_positions()
            self.logger.info(f"Current positions - RA: {ra_pos}, DEC: {dec_pos}")

            # Try to get status
            ra_status = await self.comm.get_status2_ra()
            dec_status = await self.comm.get_status2_dec()
            self.logger.info(f"Status words - RA: 0x{ra_status:04X}, DEC: 0x{dec_status:04X}")

            return True
        except Exception as e:
            self.logger.error(f"Communication test failed: {e}")
            return False

    async def find_limit_safely(self, axis: CalibrationAxis, direction: str) -> int:
        """Find a limit using hardware limit switches."""
        await self.check_shutdown()

        self.logger.info(f"🔍 Searching for {axis.value.upper()} {direction} limit...")

        # Stop and prepare
        await self.safe_stop_axis(axis)
        await asyncio.sleep(1)

        # Set target far in desired direction
        target = -150000000 if direction == "negative" else 150000000

        # Start movement using direct ROTSE III commands
        if axis == CalibrationAxis.HA:
            await self.comm.move_ra_enc_direct(target)
        else:
            await self.comm.move_dec_enc_direct(target)

        start_time = asyncio.get_event_loop().time()
        last_position = None
        stationary_count = 0

        while True:
            await self.check_shutdown()

            # Timeout check
            if asyncio.get_event_loop().time() - start_time > self.movement_timeout:
                await self.safe_stop_axis(axis)
                raise TimeoutError(f"Timeout finding {axis.value} {direction} limit")

            # Emergency stop check
            if await self.check_emergency_stop(axis):
                await self.safe_stop_axis(axis)
                raise RuntimeError(f"Emergency stop during {axis.value} {direction} limit search")

            # Check limit switches - PRIMARY SAFETY FEATURE
            neg_limit, pos_limit = await self.check_limit_switches(axis)

            if (direction == "negative" and neg_limit) or (direction == "positive" and pos_limit):
                await self.safe_stop_axis(axis)
                ra_enc, dec_enc = await self.comm.get_encoder_positions()
                position = ra_enc if axis == CalibrationAxis.HA else dec_enc
                self.logger.info(f"✓ {axis.value.upper()} {direction} limit found at {position}")
                return position

            # Backup: check for stationary condition
            ra_enc, dec_enc = await self.comm.get_encoder_positions()
            current_position = ra_enc if axis == CalibrationAxis.HA else dec_enc

            if last_position is not None:
                if abs(current_position - last_position) < self.position_tolerance:
                    stationary_count += 1
                    if stationary_count >= int(self.max_stationary_time / self.status_check_interval):
                        await self.safe_stop_axis(axis)
                        self.logger.warning(f"⚠ {axis.value.upper()} hit mechanical limit at {current_position}")
                        return current_position
                else:
                    stationary_count = 0

            last_position = current_position
            await asyncio.sleep(self.status_check_interval)

    async def move_to_safe_position(self, axis: CalibrationAxis, from_position: int, direction: str):
        """Move away from limit to safe position."""
        await self.check_shutdown()

        offset = self.limits_safety_buffer * 2
        safe_pos = from_position + offset if direction == "positive" else from_position - offset

        self.logger.info(f"Moving {axis.value.upper()} to safe position {safe_pos}")

        # Use slower speed for safety
        if axis == CalibrationAxis.HA:
            await self.comm.set_max_vel_ra(self.fine_search_velocity)
            await self.comm.move_ra_enc_direct(safe_pos)
        else:
            await self.comm.set_max_vel_dec(self.fine_search_velocity)
            await self.comm.move_dec_enc_direct(safe_pos)

        # Wait for movement to complete
        for _ in range(60):  # 30 second timeout
            await self.check_shutdown()

            if await self.check_emergency_stop(axis):
                await self.safe_stop_axis(axis)
                raise RuntimeError(f"Emergency stop during {axis.value} safety move")

            ra_enc, dec_enc = await self.comm.get_encoder_positions()
            current_pos = ra_enc if axis == CalibrationAxis.HA else dec_enc

            if abs(current_pos - safe_pos) < self.position_tolerance * 2:
                break
            await asyncio.sleep(0.5)

        await self.safe_stop_axis(axis)

    async def calibrate_axis(self, axis: CalibrationAxis) -> Tuple[int, int]:
        """Calibrate a single axis and return (negative_limit, positive_limit)."""
        self.logger.info(f"🔧 Starting {axis.value.upper()} axis calibration")

        await self.prepare_axis(axis)

        # Find negative limit
        neg_limit = await self.find_limit_safely(axis, "negative")
        await self.move_to_safe_position(axis, neg_limit, "positive")

        # Find positive limit
        pos_limit = await self.find_limit_safely(axis, "positive")
        await self.move_to_safe_position(axis, pos_limit, "negative")

        range_steps = pos_limit - neg_limit
        self.logger.info(
            f"✓ {axis.value.upper()} calibration complete: {neg_limit} to {pos_limit} ({range_steps} steps)")

        return neg_limit, pos_limit

    async def run_full_calibration(self) -> Dict[str, Any]:
        """Run complete calibration process."""
        self.logger.info("🚀 Starting ROTSE III telescope calibration")

        try:
            # Test communication first
            if not await self.test_communication():
                raise RuntimeError("Cannot communicate with ROTSE III mount")

            # Initial safety checks
            if await self.check_emergency_stop():
                self.logger.error("❌ Mount is in emergency stop - cannot start calibration")
                try:
                    faults = await self.comm.get_recent_faults()
                    self.logger.error(f"Recent faults: {faults}")
                except Exception:
                    pass
                raise RuntimeError("Emergency stop condition detected")

            print("🔍 Calibrating telescope mount...")
            print("   Press Ctrl+C at any time to safely abort")

            # Stop both axes
            await self.comm.stop()
            await asyncio.sleep(2)

            # Calibrate HA axis
            print("\n📐 Calibrating Hour Angle (HA) axis...")
            ha_neg, ha_pos = await self.calibrate_axis(CalibrationAxis.HA)

            # Calibrate DEC axis
            print("\n📐 Calibrating Declination (DEC) axis...")
            dec_neg, dec_pos = await self.calibrate_axis(CalibrationAxis.DEC)

            # Calculate parameters
            ha_range = ha_pos - ha_neg
            dec_range = dec_pos - dec_neg
            ha_steps_per_degree = ha_range / 180.0
            sidereal_rate = (ha_range * 2) / 86164.0905  # Full 360° rotation in sidereal day

            # Create calibration data
            calibration_data = {
                'calibrated': True,
                'calibration_date': datetime.now().isoformat(),
                'mount_type': 'ROTSE_III',
                'observer_latitude': self.observer_latitude,
                'limits': {
                    'ha_negative': ha_neg,
                    'ha_positive': ha_pos,
                    'dec_negative': dec_neg,
                    'dec_positive': dec_pos
                },
                'ranges': {
                    'ha_encoder_range': ha_range,
                    'dec_encoder_range': dec_range
                },
                'steps_per_degree': {
                    'ha': ha_steps_per_degree,
                    'dec': self.dec_steps_per_degree
                },
                'tracking': {
                    'sidereal_rate_ha_steps_per_sec': sidereal_rate,
                    'safety_buffer_steps': self.tracking_safety_buffer_steps
                },
                'safety': {
                    'limits_safety_factor': self.limits_safety_factor,
                    'calibration_velocities': {
                        'search': self.initial_search_velocity,
                        'fine': self.fine_search_velocity
                    }
                }
            }

            # Save configuration
            self.save_config(calibration_data)

            # Final stop
            await self.comm.stop()

            self.logger.info("🎉 Calibration completed successfully!")
            return calibration_data

        except KeyboardInterrupt:
            self.logger.info("Calibration stopped by user")
            raise
        except Exception as e:
            self.logger.error(f"❌ Calibration failed: {e}")
            try:
                await self.comm.stop()
            except Exception:
                pass
            raise

    def save_config(self, data: Dict[str, Any]):
        """Save calibration to file."""
        try:
            with open(self.config_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, indent=2)
            self.logger.info(f"✓ Configuration saved to {self.config_file}")
        except Exception as e:
            self.logger.error(f"❌ Failed to save config: {e}")
            raise

    def print_summary(self, data: Dict[str, Any]):
        """Print calibration summary."""
        print(f"\n{'=' * 60}")
        print("🎉 ROTSE III CALIBRATION COMPLETE!")
        print(f"{'=' * 60}")
        print(f"📅 Date: {data['calibration_date']}")
        print(f"🌍 Latitude: {data['observer_latitude']}°")
        print(f"📁 Config saved to: {self.config_file}")
        print()
        print("📊 AXIS RANGES:")
        print(
            f"   HA:  {data['limits']['ha_negative']:,} to {data['limits']['ha_positive']:,} ({data['ranges']['ha_encoder_range']:,} steps)")
        print(
            f"   DEC: {data['limits']['dec_negative']:,} to {data['limits']['dec_positive']:,} ({data['ranges']['dec_encoder_range']:,} steps)")
        print()
        print("⚙️  CALCULATED PARAMETERS:")
        print(f"   HA steps/degree: {data['steps_per_degree']['ha']:.1f}")
        print(f"   DEC steps/degree: {data['steps_per_degree']['dec']:.1f}")
        print(f"   Sidereal rate: {data['tracking']['sidereal_rate_ha_steps_per_sec']:.1f} steps/sec")
        print()
        print("✅ Your telescope is now calibrated and ready for pointing!")
        print(f"{'=' * 60}")


async def main():
    """Main calibration routine."""
    parser = argparse.ArgumentParser(
        description="ROTSE III Telescope Mount Calibration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python calibration.py                    # Use defaults
  python calibration.py --device /dev/ttyUSB0 --baudrate 9600
  python calibration.py --latitude -33.9 --config my_telescope.yaml

This script will safely calibrate your ROTSE III mount by finding the
encoder limits using hardware limit switches, then save all parameters
needed for telescope pointing to a YAML configuration file.
        """
    )

    parser.add_argument('--device', default='/dev/ttyS0',
                        help='Serial device (default: /dev/ttyS0)')
    parser.add_argument('--baudrate', type=int, default=9600,
                        help='Baud rate (default: 9600)')
    parser.add_argument('--config', default='telescope_config.yaml',
                        help='Output config file (default: telescope_config.yaml)')
    parser.add_argument('--latitude', type=float, default=-23.2716,
                        help='Observatory latitude in degrees (default: -23.2716 HESS)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Test communication without moving mount')

    args = parser.parse_args()

    print("🔭 ROTSE III Telescope Mount Calibration")
    print("=" * 50)
    print(f"Device: {args.device}")
    print(f"Baudrate: {args.baudrate}")
    print(f"Config file: {args.config}")
    print(f"Latitude: {args.latitude}°")

    if args.dry_run:
        print("⚠️  DRY RUN MODE - No mount movement")

    print("=" * 50)

    try:
        calibrator = TelescopeCalibrator(
            device=args.device,
            baudrate=args.baudrate,
            config_file=args.config,
            latitude=args.latitude
        )

        if args.dry_run:
            success = await calibrator.test_communication()
            if success:
                print("✓ Dry run complete - communication test passed")
            else:
                print("❌ Communication test failed")
                sys.exit(1)
            return

        # Run the calibration
        config_data = await calibrator.run_full_calibration()

        # Print summary
        calibrator.print_summary(config_data)

    except KeyboardInterrupt:
        print("\n🛑 Calibration stopped by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Calibration failed: {e}")
        print("💡 Check calibration.log for details")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())