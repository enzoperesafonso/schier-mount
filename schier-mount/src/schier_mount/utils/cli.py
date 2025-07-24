#!/usr/bin/env python3
"""
Interactive CLI for Telescope Control System
Provides commands for calibration, positioning, tracking, and monitoring.
"""

import asyncio
import cmd
import sys
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
import logging

# Import your existing modules
from calibrate_schier_mount import TelescopeCalibrator
from mount_coordinate_transformer import MountCoordinateTransformer
from comm import Comm


class TelescopeCLI(cmd.Cmd):
    """Interactive command-line interface for telescope control."""

    intro = '''
═══════════════════════════════════════════════════════════════
    🔭 TELESCOPE CONTROL SYSTEM - Interactive CLI
═══════════════════════════════════════════════════════════════
Type 'help' for available commands or 'help <command>' for details.
Type 'quit' or 'exit' to leave the program.
    '''

    prompt = '🔭 telescope> '

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600,
                 config_file: str = "telescope_config.yaml"):
        super().__init__()
        self.device = device
        self.baudrate = baudrate
        self.config_file = Path(config_file)

        # Initialize components
        self.comm = Comm(device, baudrate)
        self.calibrator = TelescopeCalibrator(device, baudrate, str(config_file))
        self.transformer: Optional[MountCoordinateTransformer] = None
        self.calibration_data: Optional[Dict[str, Any]] = None

        # Load existing calibration if available
        asyncio.run(self._load_calibration_if_exists())

        # Tracking state
        self.tracking_enabled = False
        self.sidereal_rate = -100
        self.flipped = False

        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger('telescope_cli')

    async def _load_calibration_if_exists(self):
        """Load calibration data if it exists."""
        try:
            if self.config_file.exists():
                self.calibration_data = self.calibrator.load_calibration_data()
                self.transformer = MountCoordinateTransformer(self.calibration_data)
                if self.calibration_data.get('sidereal_rate'):
                    self.sidereal_rate = self.calibration_data['sidereal_rate']
                print(f"✅ Loaded existing calibration from {self.config_file}")
                self._print_calibration_status()
            else:
                print(f"⚠️  No calibration file found at {self.config_file}")
                print("   Run 'calibrate' command to calibrate the telescope first.")
        except Exception as e:
            print(f"❌ Error loading calibration: {e}")

    def _print_calibration_status(self):
        """Print current calibration status."""
        if self.calibration_data and self.calibration_data.get('calibrated', False):
            limits = self.calibration_data['limits']
            ranges = self.calibration_data['ranges']
            print(f"""
📊 Calibration Status:
   RA Range:  {limits['ra_negative']} to {limits['ra_positive']} ({ranges['ra_encoder_range']} steps)
   DEC Range: {limits['dec_negative']} to {limits['dec_positive']} ({ranges['dec_encoder_range']} steps)
   Date: {self.calibration_data.get('calibration_date', 'Unknown')}
            """)
        else:
            print("❌ Telescope not calibrated")

    def _run_async(self, coro):
        """Helper to run async functions in sync context."""
        try:
            return asyncio.run(coro)
        except KeyboardInterrupt:
            print("\n⚠️  Operation cancelled by user")
            return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None

    async def _run_async_method(self, coro):
        """Helper to await async methods."""
        return await coro

    async def _convert_coordinates(self, ha_hours: float, dec_degrees: float):
        """Convert HA/Dec to encoder positions."""
        return await self.transformer.astro_ha_dec_to_encoder_steps(ha_hours, dec_degrees)

    # ==================== CALIBRATION COMMANDS ====================

    def do_calibrate(self, args):
        """
        Perform full telescope calibration.

        Usage: calibrate

        This will:
        1. Find encoder limits for both RA and DEC axes
        2. Save calibration data to config file
        3. Initialize coordinate transformer

        ⚠️  WARNING: This will move the telescope to its limits!
        """
        print("🔄 Starting telescope calibration...")
        print("⚠️  This will move the telescope to find its limits.")

        confirm = input("Continue? (y/N): ").lower().strip()
        if confirm != 'y':
            print("❌ Calibration cancelled")
            return

        result = self._run_async(self.calibrator.run_full_calibration())
        if result:
            self.calibration_data = result
            self.transformer = MountCoordinateTransformer(self.calibration_data)
            if self.calibration_data.get('sidereal_rate'):
                self.sidereal_rate = self.calibration_data['sidereal_rate']
            print("✅ Calibration completed successfully!")
            self._print_calibration_status()

    def do_cal_status(self, args):
        """Show current calibration status."""
        self._print_calibration_status()

    # ==================== POSITIONING COMMANDS ====================

    def do_goto_ha_dec(self, args):
        """
        Point telescope to Hour Angle and Declination coordinates.

        Usage: goto_ha_dec <ha_hours> <dec_degrees>

        Example: goto_ha_dec 2.5 45.0
                goto_ha_dec -1.2 -30.5
        """
        if not self._check_calibrated():
            return

        try:
            parts = args.split()
            if len(parts) != 2:
                print("❌ Usage: goto_ha_dec <ha_hours> <dec_degrees>")
                return

            ha_hours = float(parts[0])
            dec_degrees = float(parts[1])

            print(f"🎯 Moving to HA={ha_hours}h, Dec={dec_degrees}°...")

            # Convert to encoder positions
            coord_result = self._run_async(self._convert_coordinates(ha_hours, dec_degrees))
            if coord_result is None:
                return

            ra_enc, dec_enc = coord_result
            # set slew
            self._run_async(self.comm.set_velocity(40000,40000))
            # Move telescope
            result = self._run_async(self.comm.move_enc(ra_enc, dec_enc))
            if result is not None:
                print(f"✅ Telescope moving to encoder positions: RA={ra_enc}, DEC={dec_enc}")
                if self.transformer.under_pole_pointing:
                    print("ℹ️  Using under-pole pointing (target is beyond ±6h)")

        except ValueError as e:
            print(f"❌ {e}")
        except Exception as e:
            print(f"❌ Error: {e}")

    def do_goto_enc(self, args):
        """
        Move telescope to specific encoder positions.

        Usage: goto_enc <ra_encoder> <dec_encoder>

        Example: goto_enc 50000 -25000
        """
        if not self._check_calibrated():
            return

        try:
            parts = args.split()
            if len(parts) != 2:
                print("❌ Usage: goto_enc <ra_encoder> <dec_encoder>")
                return

            ra_enc = int(parts[0])
            dec_enc = int(parts[1])

            # Check bounds
            if not self.transformer._is_within_bounds(ra_enc, dec_enc):
                print("❌ Position is outside calibrated limits!")
                return

            print(f"🎯 Moving to encoder positions: RA={ra_enc}, DEC={dec_enc}...")
            self._run_async(self.comm.set_velocity(40000, 40000))
            result = self._run_async(self.comm.move_enc(ra_enc, dec_enc))
            if result is not None:
                print("✅ Telescope moving to target position")

        except ValueError:
            print("❌ Invalid encoder values. Use integers only.")
        except Exception as e:
            print(f"❌ Error: {e}")

    def do_home(self, args):
        """
        Send telescope to home position.

        Usage: home
        """
        print("🏠 Sending telescope to home position...")
        result = self._run_async(self.comm.home())
        if result is not None:
            print("✅ Telescope homing")

    # ==================== TRACKING COMMANDS ====================

    def do_track_start(self, args):
        """
        Enable sidereal tracking.

        Usage: track_start [sidereal_rate] [flipped]

        Parameters:
        - sidereal_rate: Rate in encoder steps/second (default: from config or 100)
        - flipped: 'true' or 'false' to flip tracking direction (default: false)

        Example: track_start
                track_start 95
                track_start 105 true
        """
        parts = args.split() if args else []

        # Parse sidereal rate
        if len(parts) >= 1:
            try:
                self.sidereal_rate = int(parts[0])
            except ValueError:
                print("❌ Invalid sidereal rate. Using default.")

        # Parse flipped flag
        if len(parts) >= 2:
            self.flipped = parts[1].lower() in ['true', '1', 'yes', 'y']

        print(f"🌟 Starting sidereal tracking...")
        print(f"   Rate: {self.sidereal_rate} steps/sec")
        print(f"   Flipped: {self.flipped}")

        result = self._run_async(
            self.comm.set_track_sidereal(self.sidereal_rate, self.flipped)
        )

        self.tracking_enabled = True
        print("✅ Sidereal tracking enabled")

    def do_track_stop(self, args):
        """
        Disable sidereal tracking.

        Usage: track_stop
        """
        print("⏹️  Stopping sidereal tracking...")
        result = self._run_async(self.comm.stop())

        self.tracking_enabled = False
        print("✅ Sidereal tracking disabled")

    def do_track_status(self, args):
        """Show current tracking status."""
        status = "🌟 ENABLED" if self.tracking_enabled else "⏹️  DISABLED"
        print(f"""
📊 Tracking Status: {status}
   Sidereal Rate: {self.sidereal_rate} steps/sec
   Flipped: {self.flipped}
        """)

    # ==================== MOTION CONTROL COMMANDS ====================

    def do_stop(self, args):
        """
        Stop all telescope motion immediately.

        Usage: stop
        """
        print("🛑 Stopping all motion...")
        result = self._run_async(self.comm.stop())
        if result is not None:
            self.tracking_enabled = False
            print("✅ All motion stopped")

    def do_set_velocity(self, args):
        """
        Set telescope movement velocity.

        Usage: set_velocity <ra_velocity> <dec_velocity>

        Velocities in steps per second.

        Example: set_velocity 5000 5000
        """
        try:
            parts = args.split()
            if len(parts) != 2:
                print("❌ Usage: set_velocity <ra_velocity> <dec_velocity>")
                return

            ra_vel = int(parts[0])
            dec_vel = int(parts[1])

            result = self._run_async(self.comm.set_velocity(ra_vel, dec_vel))
            if result is not None:
                print(f"✅ Velocity set to RA={ra_vel}, DEC={dec_vel} steps/sec")

        except ValueError:
            print("❌ Invalid velocity values. Use integers only.")
        except Exception as e:
            print(f"❌ Error: {e}")

    # ==================== STATUS AND MONITORING ====================

    def do_status(self, args):
        """
        Show current telescope status.

        Usage: status
        """
        print("📊 Getting telescope status...")

        # Get encoder positions
        result = self._run_async(self.comm.get_encoder_positions())
        if result is None:
            return

        ra_enc, dec_enc = result

        print(f"""
═══════════════════════════════════════════════════════════════
📊 TELESCOPE STATUS
═══════════════════════════════════════════════════════════════
🔧 Hardware:
   Device: {self.device}
   Baud Rate: {self.baudrate}

📍 Current Position:
   RA Encoder:  {ra_enc:>10}
   DEC Encoder: {dec_enc:>10}
        """)

        # Show astronomical coordinates if calibrated
        if self.transformer:
            try:
                mech_ha = self.transformer._encoder_to_mech_hours(ra_enc)
                mech_dec = self.transformer._encoder_to_mech_dec_degrees(dec_enc)
                astro_dec = self.transformer._mech_dec_degrees_to_astro_dec_degrees(mech_dec)

                print(f"""🌟 Astronomical Coordinates:
   Hour Angle:  {mech_ha:>8.3f} hours
   Declination: {astro_dec:>8.3f}°
                """)
            except Exception as e:
                print(f"   (Could not calculate coordinates: {e})")

        # Show tracking status
        status_icon = "🌟 ENABLED" if self.tracking_enabled else "⏹️  DISABLED"
        print(f"""🎯 Tracking:
   Status: {status_icon}
   Rate: {self.sidereal_rate} steps/sec
   Flipped: {self.flipped}
        """)

        # Show calibration status
        if self.calibration_data and self.calibration_data.get('calibrated'):
            print("✅ Calibration: VALID")
        else:
            print("❌ Calibration: NOT CALIBRATED")

        print("═══════════════════════════════════════════════════════════════")

    def do_limits(self, args):
        """Show telescope limits and ranges."""
        if not self._check_calibrated():
            return

        limits = self.calibration_data['limits']
        ranges = self.calibration_data['ranges']
        dec_limits = self.calibration_data['dec_limits']
        ra_limits = self.calibration_data['ra_limits']

        print(f"""
═══════════════════════════════════════════════════════════════
📏 TELESCOPE LIMITS
═══════════════════════════════════════════════════════════════
🔧 Encoder Limits:
   RA:  {limits['ra_negative']:>10} to {limits['ra_positive']:>10} ({ranges['ra_encoder_range']:>10} steps)
   DEC: {limits['dec_negative']:>10} to {limits['dec_positive']:>10} ({ranges['dec_encoder_range']:>10} steps)

🌟 Mechanical Limits:
   RA Hours:  {ra_limits['negative_hours']:>6.1f}h to {ra_limits['positive_hours']:>6.1f}h ({ra_limits['ra_angular_range']:>6.1f}h range)
   DEC Degrees: {dec_limits['negative_degrees']:>6.1f}° to {dec_limits['positive_degrees']:>6.1f}° ({dec_limits['dec_angular_range']:>6.1f}° range)

🛡️  Safety Buffer: {self.calibration_data.get('limits_safety_buffer', 'Unknown')} encoder steps
═══════════════════════════════════════════════════════════════
        """)

    # ==================== UTILITY COMMANDS ====================

    def do_config(self, args):
        """
        Show or modify configuration.

        Usage: config [show|device|baudrate|sidereal_rate] [value]

        Examples: config show
                 config device /dev/ttyUSB0
                 config baudrate 19200
                 config sidereal_rate 95
        """
        parts = args.split() if args else ['show']

        if not parts or parts[0] == 'show':
            print(f"""
📋 Current Configuration:
   Device: {self.device}
   Baud Rate: {self.baudrate}
   Config File: {self.config_file}
   Sidereal Rate: {self.sidereal_rate}
            """)

        elif parts[0] == 'device' and len(parts) == 2:
            self.device = parts[1]
            print(f"✅ Device set to: {self.device}")
            print("ℹ️  Restart CLI to apply device change")

        elif parts[0] == 'baudrate' and len(parts) == 2:
            try:
                self.baudrate = int(parts[1])
                print(f"✅ Baud rate set to: {self.baudrate}")
                print("ℹ️  Restart CLI to apply baud rate change")
            except ValueError:
                print("❌ Invalid baud rate")

        elif parts[0] == 'sidereal_rate' and len(parts) == 2:
            try:
                self.sidereal_rate = int(parts[1])
                print(f"✅ Sidereal rate set to: {self.sidereal_rate}")
            except ValueError:
                print("❌ Invalid sidereal rate")

        else:
            print("❌ Usage: config [show|device|baudrate|sidereal_rate] [value]")

    def _check_calibrated(self):
        """Check if telescope is calibrated."""
        if not self.calibration_data or not self.calibration_data.get('calibrated', False):
            print("❌ Telescope not calibrated! Run 'calibrate' command first.")
            return False
        return True

    # ==================== CMD FRAMEWORK OVERRIDES ====================

    def do_quit(self, args):
        """Exit the telescope control system."""
        print("👋 Goodbye! Stopping telescope and exiting...")
        self._run_async(self.comm.stop())
        return True

    def do_exit(self, args):
        """Exit the telescope control system."""
        return self.do_quit(args)

    def do_EOF(self, args):
        """Handle Ctrl+D to exit."""
        print()  # New line after ^D
        return self.do_quit(args)

    def emptyline(self):
        """Override to do nothing on empty line."""
        pass

    def default(self, line):
        """Handle unknown commands."""
        print(f"❌ Unknown command: '{line}'. Type 'help' for available commands.")

    def cmdloop(self, intro=None):
        """Override to handle KeyboardInterrupt gracefully."""
        try:
            super().cmdloop(intro)
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            self._run_async(self.comm.stop())


def main():
    """Main entry point for the CLI."""
    import argparse

    parser = argparse.ArgumentParser(description='Interactive Telescope Control CLI')
    parser.add_argument('--device', default='/dev/ttyS0',
                        help='Serial device path (default: /dev/ttyS0)')
    parser.add_argument('--baudrate', type=int, default=9600,
                        help='Serial baud rate (default: 9600)')
    parser.add_argument('--config', default='telescope_config.yaml',
                        help='Configuration file path (default: telescope_config.yaml)')

    args = parser.parse_args()

    print(f"🔭 Initializing telescope control system...")
    print(f"   Device: {args.device}")
    print(f"   Baud Rate: {args.baudrate}")
    print(f"   Config: {args.config}")

    try:
        cli = TelescopeCLI(args.device, args.baudrate, args.config)
        cli.cmdloop()
    except Exception as e:
        print(f"❌ Failed to initialize telescope control: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()