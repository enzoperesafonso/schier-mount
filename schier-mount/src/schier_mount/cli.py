#!/usr/bin/env python3
"""
Telescope Mount CLI
A command-line interface for telescope calibration and pointing operations.
"""

import asyncio
import argparse
import sys
import json
import yaml
from pathlib import Path
from typing import Optional, Dict, Any
import logging
from datetime import datetime

# Import the telescope modules (assuming they're in the same directory)
try:
    from utils.calibration import Calibration, CalibrationConfig, CalibrationError, CalibrationProgress
    from utils.pointing import SchierMountPointing
    from utils.comm import Comm
except ImportError as e:
    print(f"Error importing telescope modules: {e}")
    print("Make sure calibration.py and pointing.py are in the same directory as this script.")
    sys.exit(1)


class MockMount:
    """Mock mount communication for testing purposes."""

    # def __init__(self):
    #     self.ra_position = 0
    #     self.dec_position = 0
    #     self.velocity_ra = 40000
    #     self.velocity_dec = 40000
    #     self.stopped = False
    #
    # async def get_encoder_positions(self):
    #     """Return current encoder positions."""
    #     return self.ra_position, self.dec_position
    #
    # async def move_ra(self, position):
    #     """Move RA axis to position."""
    #     print(f"Moving RA to position {position}")
    #     self.ra_position = position
    #     await asyncio.sleep(0.1)  # Simulate movement time
    #
    # async def move_dec(self, position):
    #     """Move DEC axis to position."""
    #     print(f"Moving DEC to position {position}")
    #     self.dec_position = position
    #     await asyncio.sleep(0.1)  # Simulate movement time
    #
    # async def move_ra_enc(self, position):
    #     """Move RA axis to encoder position."""
    #     print(f"Moving RA to encoder position {position}")
    #     self.ra_position = position
    #     await asyncio.sleep(0.1)
    #
    # async def move_dec_enc(self, position):
    #     """Move DEC axis to encoder position."""
    #     print(f"Moving DEC to encoder position {position}")
    #     self.dec_position = position
    #     await asyncio.sleep(0.1)
    #
    # async def stop(self):
    #     """Stop all motion."""
    #     print("Stopping mount motion")
    #     self.stopped = True
    #     await asyncio.sleep(0.1)
    #
    # async def set_velocity(self, ra_vel, dec_vel):
    #     """Set axis velocities."""
    #     print(f"Setting velocities: RA={ra_vel}, DEC={dec_vel}")
    #     self.velocity_ra = ra_vel
    #     self.velocity_dec = dec_vel


class TelescopeCLI:
    """Main CLI class for telescope operations."""

    def __init__(self):
        self.mount = Comm()  # Replace with real mount communication
        self.calibrator = None
        self.pointer = None
        self.config_file = "telescope_config.yaml"

        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler('telescope_cli.log')
            ]
        )
        self.logger = logging.getLogger(__name__)

    def setup_calibrator(self, config_params: Optional[Dict] = None):
        """Initialize the calibrator with optional config parameters."""
        if config_params:
            config = CalibrationConfig(**config_params)
        else:
            config = CalibrationConfig()

        self.calibrator = Calibration(self.mount, config)

        # Try to load existing configuration
        if Path(self.config_file).exists():
            try:
                self.calibrator.load_config_yaml(self.config_file)
                print(f"✓ Loaded existing configuration from {self.config_file}")
                self._setup_pointer()
            except Exception as e:
                print(f"⚠ Warning: Could not load config file: {e}")

    def _setup_pointer(self):
        """Setup the pointing system if calibrated."""
        if self.calibrator and self.calibrator.is_calibrated:
            config_data = self.calibrator.get_config_data()
            # Add dec_limits to config_data for pointing system
            config_data['dec_limits'] = {
                'positive_degrees': 122.0,
                'negative_degrees': -113.0,
                'dec_angular_range': 235.0
            }
            self.pointer = SchierMountPointing(config_data)
            print("✓ Pointing system initialized")

    def progress_callback(self, progress: CalibrationProgress):
        """Callback for calibration progress updates."""
        print(f"\r[{progress.progress_percent:5.1f}%] {progress.current_operation}", end="", flush=True)

    async def cmd_calibrate(self, args):
        """Run telescope calibration."""
        print("Starting telescope calibration...")
        print("This will move the mount to find all limit switches.")

        response = input("Continue? (y/N): ").strip().lower()
        if response != 'y':
            print("Calibration cancelled.")
            return

        if not self.calibrator:
            self.setup_calibrator()

        try:
            print("\nRunning calibration sequence...")
            await self.calibrator.calibrate(self.progress_callback)
            print("\n✓ Calibration completed successfully!")

            # Save configuration
            self.calibrator.save_config_yaml(self.config_file)
            print(f"✓ Configuration saved to {self.config_file}")

            # Setup pointing system
            self._setup_pointer()

            # Show summary
            await self.cmd_status(args)

        except CalibrationError as e:
            print(f"\n✗ Calibration failed: {e}")
        except KeyboardInterrupt:
            print("\n\nCalibration interrupted by user")
            await self.calibrator.emergency_stop()

    async def cmd_status(self, args):
        """Show telescope status and calibration info."""
        print("\n" + "=" * 60)
        print("TELESCOPE STATUS")
        print("=" * 60)

        # Mount position
        ra_enc, dec_enc = await self.mount.get_encoder_positions()
        print(f"Current Position:")
        print(f"  RA Encoder:  {ra_enc:,}")
        print(f"  DEC Encoder: {dec_enc:,}")

        if not self.calibrator:
            print("\nCalibration: Not initialized")
            return

        # Calibration status
        progress = self.calibrator.get_progress()
        print(f"\nCalibration Status: {progress.status.value}")

        if self.calibrator.is_calibrated:
            limits_summary = self.calibrator.get_limits_summary()

            print(f"Calibration Date: {limits_summary['calibration_date']}")
            print(f"\nEncoder Ranges:")
            print(f"  RA Range:  {limits_summary['ra_range_steps']:,} steps")
            print(f"  DEC Range: {limits_summary['dec_range_steps']:,} steps")

            print(f"\nRA Limits (±6 hours):")
            print(f"  West  (-6h): {limits_summary['ra_limits']['negative_encoder']:,}")
            print(f"  East  (+6h): {limits_summary['ra_limits']['positive_encoder']:,}")

            print(f"\nDEC Limits:")
            print(
                f"  South ({limits_summary['dec_limits']['negative_degrees']:+.0f}°): {limits_summary['dec_limits']['negative_encoder']:,}")
            print(
                f"  North ({limits_summary['dec_limits']['positive_degrees']:+.0f}°): {limits_summary['dec_limits']['positive_encoder']:,}")

            print(f"\nConversion Factors:")
            print(f"  DEC: {limits_summary['conversions']['dec_degrees_per_step']:.6f} degrees/step")
            print(f"  RA:  {limits_summary['conversions']['ra_hours_per_step']:.6f} hours/step")

            # Current astronomical position if pointing system is available
            if self.pointer:
                try:
                    mech_ha = self.pointer._encoder_to_mech_hours(ra_enc)
                    mech_dec = self.pointer._encoder_to_mech_dec_degrees(dec_enc)
                    astro_dec = self.pointer._mech_dec_degrees_to_astro_dec_degrees(mech_dec)

                    print(f"\nCurrent Astronomical Position:")
                    print(f"  Hour Angle: {mech_ha:+.3f} hours")
                    print(f"  Declination: {astro_dec:+.1f}°")

                    if self.pointer.under_pole_pointing:
                        print("  ⚠ Under-pole pointing active")

                except Exception as e:
                    print(f"\n⚠ Error calculating current position: {e}")
        else:
            print("Telescope is not calibrated.")
            print("Run 'calibrate' command to calibrate the mount.")

    async def cmd_goto(self, args):
        """Go to specified coordinates."""
        if not self.pointer:
            print("✗ Telescope not calibrated or pointing system not available.")
            print("Run 'calibrate' command first.")
            return

        ha_hours = args.ha
        dec_degrees = args.dec

        print(f"Going to HA={ha_hours:+.3f}h, DEC={dec_degrees:+.1f}°")

        try:
            success = await self.pointer.astro_ha_dec_to_encoder_steps(
                ha_hours, dec_degrees, wait_for_completion=True
            )

            if success:
                print("✓ Goto completed successfully")
                if self.pointer.under_pole_pointing:
                    print("ℹ Using under-pole pointing for this target")
            else:
                print("✗ Goto failed")

        except ValueError as e:
            print(f"✗ Goto failed: {e}")
        except Exception as e:
            print(f"✗ Unexpected error during goto: {e}")

    async def cmd_move(self, args):
        """Move mount by encoder steps."""
        print(f"Moving RA by {args.ra_steps} steps, DEC by {args.dec_steps} steps")

        try:
            current_ra, current_dec = await self.mount.get_encoder_positions()

            if args.ra_steps != 0:
                new_ra = current_ra + args.ra_steps
                await self.mount.move_ra_enc(new_ra)
                print(f"RA moved to {new_ra}")

            if args.dec_steps != 0:
                new_dec = current_dec + args.dec_steps
                await self.mount.move_dec_enc(new_dec)
                print(f"DEC moved to {new_dec}")

        except Exception as e:
            print(f"✗ Move failed: {e}")

    async def cmd_stop(self, args):
        """Stop all mount motion."""
        print("Stopping mount motion...")
        await self.mount.stop()
        print("✓ Mount stopped")

    async def cmd_config(self, args):
        """Show or modify configuration."""
        if args.action == 'show':
            if self.calibrator and self.calibrator.is_calibrated:
                config_data = self.calibrator.get_config_data()
                print("\nCurrent Configuration:")
                print(yaml.dump(config_data, default_flow_style=False, indent=2))
            else:
                print("No configuration available. Run calibration first.")

        elif args.action == 'save':
            if not self.calibrator or not self.calibrator.is_calibrated:
                print("✗ No calibration data to save")
                return

            filepath = args.file or self.config_file
            self.calibrator.save_config_yaml(filepath)
            print(f"✓ Configuration saved to {filepath}")

        elif args.action == 'load':
            filepath = args.file or self.config_file
            if not Path(filepath).exists():
                print(f"✗ Configuration file {filepath} not found")
                return

            if not self.calibrator:
                self.setup_calibrator()

            try:
                self.calibrator.load_config_yaml(filepath)
                print(f"✓ Configuration loaded from {filepath}")
                self._setup_pointer()
            except Exception as e:
                print(f"✗ Failed to load configuration: {e}")

    async def cmd_reset(self, args):
        """Reset calibration data."""
        print("This will clear all calibration data.")
        response = input("Are you sure? (y/N): ").strip().lower()

        if response == 'y':
            if self.calibrator:
                self.calibrator.reset_calibration()
            self.pointer = None
            print("✓ Calibration data reset")
        else:
            print("Reset cancelled")


def create_parser():
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        description="Telescope Mount CLI - Control and calibrate your telescope mount",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s calibrate                    # Run full calibration
  %(prog)s status                       # Show current status
  %(prog)s goto -ha 2.5 -dec 45        # Go to HA=2.5h, DEC=45°
  %(prog)s move -ra 1000 -dec -500      # Move by encoder steps
  %(prog)s config show                  # Show current configuration
  %(prog)s stop                         # Emergency stop
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Calibrate command
    cal_parser = subparsers.add_parser('calibrate', help='Run telescope calibration')

    # Status command
    status_parser = subparsers.add_parser('status', help='Show telescope status')

    # Goto command
    goto_parser = subparsers.add_parser('goto', help='Go to coordinates')
    goto_parser.add_argument('-ha', '--ha', type=float, required=True,
                             help='Hour angle in hours (-12 to +12)')
    goto_parser.add_argument('-dec', '--dec', type=float, required=True,
                             help='Declination in degrees (-90 to +90)')

    # Move command
    move_parser = subparsers.add_parser('move', help='Move by encoder steps')
    move_parser.add_argument('-ra', '--ra-steps', type=int, default=0,
                             help='RA encoder steps to move (+ or -)')
    move_parser.add_argument('-dec', '--dec-steps', type=int, default=0,
                             help='DEC encoder steps to move (+ or -)')

    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop all motion')

    # Config command
    config_parser = subparsers.add_parser('config', help='Configuration management')
    config_subparsers = config_parser.add_subparsers(dest='action', help='Config actions')

    config_show = config_subparsers.add_parser('show', help='Show current configuration')

    config_save = config_subparsers.add_parser('save', help='Save configuration to file')
    config_save.add_argument('-f', '--file', help='Configuration file path')

    config_load = config_subparsers.add_parser('load', help='Load configuration from file')
    config_load.add_argument('-f', '--file', help='Configuration file path')

    # Reset command
    reset_parser = subparsers.add_parser('reset', help='Reset calibration data')

    return parser


async def main():
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    cli = TelescopeCLI()

    # Command dispatch
    command_map = {
        'calibrate': cli.cmd_calibrate,
        'status': cli.cmd_status,
        'goto': cli.cmd_goto,
        'move': cli.cmd_move,
        'stop': cli.cmd_stop,
        'config': cli.cmd_config,
        'reset': cli.cmd_reset,
    }

    try:
        command_func = command_map.get(args.command)
        if command_func:
            await command_func(args)
        else:
            print(f"Unknown command: {args.command}")
            parser.print_help()

    except KeyboardInterrupt:
        print("\nOperation interrupted by user")
        await cli.mount.stop()
    except Exception as e:
        print(f"Unexpected error: {e}")
        cli.logger.exception("Unexpected error in main()")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)