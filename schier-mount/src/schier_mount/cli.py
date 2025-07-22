#!/usr/bin/env python3
"""
Telescope Control CLI
A command-line interface for controlling a fork-mounted equatorial telescope
"""

import asyncio
import argparse
import sys
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any
import signal

# Import your telescope modules
from utils.calibration import Calibration, CalibrationProgress
from utils.pointing import SchierMountPointing
from utils.comm import Comm


class MockComm:
    """Mock communication class for testing - replace with your actual comm class"""

    def __init__(self):
        self.ra_pos = 0
        self.dec_pos = 0
        self.moving = False

    async def get_encoder_positions(self):
        return self.ra_pos, self.dec_pos

    async def move_ra_enc(self, position):
        print(f"Moving RA to encoder position: {position}")
        self.ra_pos = position

    async def move_dec_enc(self, position):
        print(f"Moving Dec to encoder position: {position}")
        self.dec_pos = position

    async def move_enc(self, ra_pos, dec_pos):
        print(f"Moving to RA: {ra_pos}, Dec: {dec_pos}")
        self.ra_pos = ra_pos
        self.dec_pos = dec_pos

    async def stop(self):
        print("Stopping all motion")
        self.moving = False

    async def set_velocity(self, ra_vel, dec_vel):
        print(f"Setting velocities - RA: {ra_vel}, Dec: {dec_vel}")


class TelescopeController:
    """Main telescope controller class"""

    def __init__(self, config_file: str = "telescope_config.json"):
        self.config_file = config_file
        self.config = self.load_config()

        # Initialize communication (replace MockComm with your actual comm class)
        self.comm = Comm # MockComm()

        # Initialize calibration
        self.calibration = Calibration(
            self.comm,
            observatory_latitude=self.config.get('observatory_latitude', -22.9)
        )

        # Initialize pointing (will be set after calibration)
        self.pointing = None

        # Load existing calibration if available
        self.load_calibration()

    def load_config(self) -> Dict[str, Any]:
        """Load configuration from file"""
        default_config = {
            'observatory_latitude': -22.9,  # Namibia
            'motion_timeout': 120.0,
            'fast_velocity': 70000,
            'slow_velocity': 5000,
            'home_velocity': 65000
        }

        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    default_config.update(config)
            except Exception as e:
                print(f"Warning: Could not load config file: {e}")

        return default_config

    def save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"Error saving config: {e}")

    def load_calibration(self):
        """Load calibration data from file"""
        cal_file = "calibration_data.json"
        if os.path.exists(cal_file):
            try:
                with open(cal_file, 'r') as f:
                    cal_data = json.load(f)
                    self.calibration._calibration_data = cal_data

                    # Initialize pointing if calibrated
                    if cal_data.get('calibrated'):
                        self.pointing = SchierMountPointing(
                            self.comm,
                            cal_data,
                            self.config['observatory_latitude']
                        )
                        print("Loaded existing calibration data")

            except Exception as e:
                print(f"Warning: Could not load calibration data: {e}")

    def save_calibration(self, cal_data: Dict):
        """Save calibration data to file"""
        try:
            with open("calibration_data.json", 'w') as f:
                json.dump(cal_data, f, indent=2)
            print("Calibration data saved")
        except Exception as e:
            print(f"Error saving calibration data: {e}")


def progress_callback(progress: CalibrationProgress):
    """Callback function for calibration progress updates"""
    status_char = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    char_idx = int(progress.progress_percent / 10) % len(status_char)

    print(f"\r{status_char[char_idx]} [{progress.progress_percent:5.1f}%] "
          f"{progress.phase.value}: {progress.current_operation}", end="", flush=True)

    if progress.status.value in ['completed', 'failed']:
        print()  # New line when done


async def cmd_status(controller: TelescopeController, args):
    """Show telescope status"""
    print("=" * 50)
    print("TELESCOPE STATUS")
    print("=" * 50)

    # Calibration status
    if controller.calibration.is_calibrated:
        print("✓ Calibrated")
        cal_data = controller.calibration.calibration_data
        print(f"  Calibrated on: {cal_data.get('calibration_date', 'Unknown')}")

        # Show limits summary
        limits = controller.calibration.get_limits_summary()
        print(f"  RA Range: {limits['ra_range_steps']:,} steps")
        print(f"  Dec Range: {limits['dec_range_steps']:,} steps")
        print(f"  HA Limits: {limits['ra_limits']['negative_hours']}h to {limits['ra_limits']['positive_hours']}h")
        print(
            f"  Dec Limits: {limits['dec_limits']['negative_degrees']:.1f}° to {limits['dec_limits']['positive_degrees']:.1f}°")

        # Current position
        if controller.pointing:
            ha, dec = await controller.pointing.get_ha_dec()
            print(f"\nCurrent Position:")
            print(f"  Hour Angle: {ha:+.3f}h")
            print(f"  Declination: {dec:+.2f}°")

    else:
        print("✗ Not calibrated")

    # Encoder positions
    ra_enc, dec_enc = await controller.comm.get_encoder_positions()
    print(f"\nEncoder Positions:")
    print(f"  RA: {ra_enc:,}")
    print(f"  Dec: {dec_enc:,}")

    print(f"\nObservatory Latitude: {controller.config['observatory_latitude']}°")


async def cmd_calibrate(controller: TelescopeController, args):
    """Run telescope calibration"""
    print("Starting telescope calibration...")
    print("This will move the telescope to find all mechanical limits.")

    if not args.force and controller.calibration.is_calibrated:
        response = input("Telescope is already calibrated. Continue anyway? (y/N): ")
        if response.lower() != 'y':
            print("Calibration cancelled")
            return

    try:
        print("\nCalibration in progress...")
        cal_data = await controller.calibration.calibrate(progress_callback)

        # Save calibration data
        controller.save_calibration(cal_data)

        # Initialize pointing
        controller.pointing = SchierMountPointing(
            controller.comm,
            cal_data,
            controller.config['observatory_latitude']
        )

        print(f"\n✓ Calibration completed successfully!")

        # Show summary
        limits = controller.calibration.get_limits_summary()
        print(f"RA Range: {limits['ra_range_steps']:,} steps")
        print(f"Dec Range: {limits['dec_range_steps']:,} steps")

    except Exception as e:
        print(f"\n✗ Calibration failed: {e}")


async def cmd_goto(controller: TelescopeController, args):
    """Go to specified coordinates"""
    if not controller.pointing:
        print("Error: Telescope not calibrated. Run 'calibrate' first.")
        return

    try:
        print(f"Moving to HA: {args.ha:+.3f}h, Dec: {args.dec:+.2f}°")

        success = await controller.pointing.goto_ha_dec(
            args.ha,
            args.dec,
            wait_for_completion=not args.no_wait
        )

        if success:
            print("✓ Move completed")
            ha, dec = await controller.pointing.get_ha_dec()
            print(f"Current position: HA {ha:+.3f}h, Dec {dec:+.2f}°")
        else:
            print("✗ Move failed")

    except ValueError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")


async def cmd_stop(controller: TelescopeController, args):
    """Stop all telescope motion"""
    print("Stopping telescope motion...")
    await controller.comm.stop()
    print("✓ Motion stopped")


async def cmd_home(controller: TelescopeController, args):
    """Move telescope to home position"""
    if not controller.pointing:
        print("Error: Telescope not calibrated. Run 'calibrate' first.")
        return

    try:
        print("Moving to home position (HA: 0h, Dec: 0°)...")
        success = await controller.pointing.goto_ha_dec(0.0, 0.0)

        if success:
            print("✓ Telescope homed")
        else:
            print("✗ Homing failed")

    except Exception as e:
        print(f"Error: {e}")


async def cmd_limits(controller: TelescopeController, args):
    """Show telescope limits and ranges"""
    if not controller.calibration.is_calibrated:
        print("Error: Telescope not calibrated")
        return

    limits = controller.calibration.get_limits_summary()

    print("=" * 50)
    print("TELESCOPE LIMITS")
    print("=" * 50)

    print(f"Hour Angle Range: {limits['ra_limits']['negative_hours']}h to {limits['ra_limits']['positive_hours']}h")
    print(
        f"Declination Range: {limits['dec_limits']['negative_degrees']:.1f}° to {limits['dec_limits']['positive_degrees']:.1f}°")
    print(f"Total Dec Angular Range: {limits['dec_limits']['angular_range_from_scp']}° from SCP")

    print(f"\nEncoder Ranges:")
    print(f"RA: {limits['ra_range_steps']:,} steps")
    print(f"Dec: {limits['dec_range_steps']:,} steps")

    print(f"\nEncoder Limits:")
    print(f"RA: {limits['ra_limits']['negative_encoder']:,} to {limits['ra_limits']['positive_encoder']:,}")
    print(f"Dec: {limits['dec_limits']['negative_encoder']:,} to {limits['dec_limits']['positive_encoder']:,}")

    print(f"\nHome Position:")
    print(f"RA: {limits['home_position']['ra_encoder']:,}")
    print(f"Dec: {limits['home_position']['dec_encoder']:,}")


async def cmd_config(controller: TelescopeController, args):
    """Configure telescope parameters"""
    if args.set:
        key, value = args.set.split('=', 1)
        try:
            # Try to convert to appropriate type
            if '.' in value:
                value = float(value)
            else:
                value = int(value)
        except ValueError:
            pass  # Keep as string

        controller.config[key] = value
        controller.save_config()
        print(f"Set {key} = {value}")

    else:
        print("Current Configuration:")
        print("=" * 30)
        for key, value in controller.config.items():
            print(f"{key}: {value}")


def setup_signal_handlers(controller):
    """Setup signal handlers for graceful shutdown"""

    def signal_handler(signum, frame):
        print("\nReceived interrupt signal. Stopping telescope...")
        asyncio.create_task(controller.comm.stop())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


async def main():
    parser = argparse.ArgumentParser(description="Telescope Control CLI")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Status command
    subparsers.add_parser('status', help='Show telescope status')

    # Calibration command
    cal_parser = subparsers.add_parser('calibrate', help='Run telescope calibration')
    cal_parser.add_argument('--force', action='store_true', help='Force calibration even if already calibrated')

    # Goto command
    goto_parser = subparsers.add_parser('goto', help='Move to specified coordinates')
    goto_parser.add_argument('ha', type=float, help='Hour Angle in hours (-6 to +6)')
    goto_parser.add_argument('dec', type=float, help='Declination in degrees')
    goto_parser.add_argument('--no-wait', action='store_true', help='Don\'t wait for motion to complete')

    # Stop command
    subparsers.add_parser('stop', help='Stop all telescope motion')

    # Home command
    subparsers.add_parser('home', help='Move telescope to home position')

    # Limits command
    subparsers.add_parser('limits', help='Show telescope limits and ranges')

    # Config command
    config_parser = subparsers.add_parser('config', help='Configure telescope parameters')
    config_parser.add_argument('--set', help='Set configuration value (key=value)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Initialize controller
    controller = TelescopeController()
    setup_signal_handlers(controller)

    # Command dispatch
    commands = {
        'status': cmd_status,
        'calibrate': cmd_calibrate,
        'goto': cmd_goto,
        'stop': cmd_stop,
        'home': cmd_home,
        'limits': cmd_limits,
        'config': cmd_config,
    }

    if args.command in commands:
        await commands[args.command](controller, args)
    else:
        print(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)