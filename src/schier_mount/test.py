#!/usr/bin/env python3
"""
Interactive telescope control script with slewing capabilities
"""

import asyncio
import yaml
import logging
import sys
from pathlib import Path

# Import your telescope mount classes
try:
    from schier_mount import TelescopeMount
except ImportError as e:
    print(f"Error importing telescope_mount: {e}")
    print("Make sure schier_mount.py is in the current directory")
    exit(1)

# Simple logging setup
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def print_position_info(mount, config):
    """Helper function to print current position information"""
    current_ha, current_dec = mount.get_current_position()

    if current_ha is not None and current_dec is not None:
        print(f"\nCurrent Position:")
        print(f"  Hour Angle: {current_ha:.4f} hours ({current_ha * 15:.3f}°)")
        print(f"  Declination: {current_dec:.4f}°")

        # Convert HA to more readable format
        ha_hours = int(current_ha)
        ha_minutes = int(abs(current_ha - ha_hours) * 60)
        ha_seconds = (abs(current_ha - ha_hours) * 60 - ha_minutes) * 60

        dec_degrees = int(current_dec)
        dec_arcmin = int(abs(current_dec - dec_degrees) * 60)
        dec_arcsec = (abs(current_dec - dec_degrees) * 60 - dec_arcmin) * 60

        print(
            f"  Formatted: HA={ha_hours:+03d}h{ha_minutes:02d}m{ha_seconds:04.1f}s, Dec={dec_degrees:+03d}°{dec_arcmin:02d}'{dec_arcsec:04.1f}\"")

        # Check if position needs meridian flip
        if mount.coordinates.needs_meridian_flip(current_ha, current_dec):
            flip_ha, flip_dec = mount.coordinates.get_flip_position(current_ha, current_dec)
            print(f"  ⚠️  Beyond 6h limit - flip position: HA={flip_ha:.3f}h, Dec={flip_dec:.3f}°")

    else:
        print("Could not get current position - no encoder data available")

    # Get status information
    status = mount.get_status()
    print(f"\nMount Status:")
    print(f"  State: {status.state.value}")
    print(f"  Tracking Mode: {status.tracking_mode.value}")
    print(f"  Is Moving: {status.is_moving}")
    print(f"  Pier Side: {status.pier_side.value}")

    if status.ra_encoder is not None and status.dec_encoder is not None:
        print(f"  Raw Encoders: HA={status.ra_encoder}, Dec={status.dec_encoder}")


def get_user_coordinates():
    """Get target coordinates from user input"""
    print("\nEnter target coordinates:")

    while True:
        try:
            # Get Hour Angle
            ha_input = input("Hour Angle (hours, -12 to +12): ").strip()
            if ha_input.lower() == 'q':
                return None, None
            target_ha = float(ha_input)

            if not (-12.0 <= target_ha <= 12.0):
                print("Hour Angle must be between -12 and +12 hours")
                continue

            # Get Declination
            dec_input = input("Declination (degrees, -90 to +90): ").strip()
            if dec_input.lower() == 'q':
                return None, None
            target_dec = float(dec_input)

            if not (-90.0 <= target_dec <= 90.0):
                print("Declination must be between -90 and +90 degrees")
                continue

            return target_ha, target_dec

        except ValueError:
            print("Please enter valid numbers (or 'q' to quit)")
        except KeyboardInterrupt:
            return None, None


def show_preset_targets():
    """Show some preset targets for easy testing"""
    presets = {
        '1': (0.0, 0.0, "Celestial Equator at meridian"),
        '2': (3.0, 30.0, "3h east, +30° dec"),
        '3': (-3.0, 30.0, "3h west, +30° dec"),
        '4': (6.0, 45.0, "6h east limit, +45° dec"),
        '5': (-6.0, 45.0, "6h west limit, +45° dec"),
        '6': (8.0, 30.0, "8h east (below-pole), +30° dec"),
        '7': (-8.0, 30.0, "8h west (below-pole), +30° dec"),
        '8': (0.0, 60.0, "High declination target"),
        '9': (0.0, -30.0, "Southern target"),
    }

    print("\nPreset Targets:")
    for key, (ha, dec, desc) in presets.items():
        print(f"  {key}: HA={ha:+.1f}h, Dec={dec:+.1f}° ({desc})")

    return presets


async def perform_slew(mount, target_ha, target_dec):
    """Perform slew operation with progress monitoring"""
    print(f"\n🎯 Slewing to HA={target_ha:.3f}h, Dec={target_dec:.3f}°")

    # Check if position is reachable
    if not mount.coordinates.is_position_reachable(target_ha, target_dec):
        print("❌ Target position is not mechanically reachable!")
        return False

    # Show coordinate transformation
    try:
        ha_enc, dec_enc, below_pole = mount.coordinates.ha_dec_to_encoder_positions(target_ha, target_dec)
        print(f"   Target encoders: HA={ha_enc}, Dec={dec_enc}")
        print(f"   Mode: {'Below-pole' if below_pole else 'Normal'}")
    except Exception as e:
        print(f"❌ Coordinate transformation failed: {e}")
        return False

    # Start slew
    start_time = asyncio.get_event_loop().time()
    success = await mount.slew_to_ha_dec(target_ha, target_dec)
    end_time = asyncio.get_event_loop().time()

    if success:
        print(f"✅ Slew completed successfully in {end_time - start_time:.1f} seconds")

        # Wait for position update and show final position
        await asyncio.sleep(1)
        current_ha, current_dec = mount.get_current_position()

        if current_ha is not None and current_dec is not None:
            ha_error = abs(current_ha - target_ha)
            dec_error = abs(current_dec - target_dec)
            print(f"   Final position: HA={current_ha:.4f}h, Dec={current_dec:.4f}°")
            print(
                f"   Pointing error: HA={ha_error:.4f}h ({ha_error * 15 * 3600:.1f}\"), Dec={dec_error:.4f}° ({dec_error * 3600:.1f}\")")

        return True
    else:
        print(f"❌ Slew failed after {end_time - start_time:.1f} seconds")
        return False


async def interactive_mode(mount, config):
    """Interactive control mode"""
    print("\n" + "=" * 60)
    print("🔭 INTERACTIVE TELESCOPE CONTROL")
    print("=" * 60)

    while True:
        try:
            print_position_info(mount, config)

            print("\nOptions:")
            print("  1. Slew to custom coordinates")
            print("  2. Slew to preset target")
            print("  3. Start sidereal tracking")
            print("  4. Stop tracking")
            print("  5. Emergency stop")
            print("  6. Home telescope")
            print("  7. Refresh position")
            print("  q. Quit")

            choice = input("\nSelect option: ").strip().lower()

            if choice == 'q' or choice == 'quit':
                break

            elif choice == '1':
                # Custom coordinates
                target_ha, target_dec = get_user_coordinates()
                if target_ha is not None:
                    await perform_slew(mount, target_ha, target_dec)

            elif choice == '2':
                # Preset targets
                presets = show_preset_targets()
                preset_choice = input("\nSelect preset (1-9): ").strip()

                if preset_choice in presets:
                    target_ha, target_dec, desc = presets[preset_choice]
                    print(f"Selected: {desc}")
                    confirm = input("Confirm slew? (y/N): ").strip().lower()
                    if confirm == 'y':
                        await perform_slew(mount, target_ha, target_dec)

            elif choice == '3':
                # Start tracking
                print("\n🌟 Starting sidereal tracking...")
                success = await mount.start_sidereal_tracking()
                if success:
                    print("✅ Sidereal tracking started")
                else:
                    print("❌ Failed to start tracking")

            elif choice == '4':
                # Stop tracking
                print("\n⏹️  Stopping tracking...")
                await mount.stop_tracking()
                print("✅ Tracking stopped")

            elif choice == '5':
                # Emergency stop
                print("\n🚨 EMERGENCY STOP!")
                await mount.emergency_stop()
                print("✅ Emergency stop executed")

            elif choice == '6':
                # Home telescope
                print("\n🏠 Homing telescope (this may take a while)...")
                success = await mount.home()
                if success:
                    print("✅ Homing completed")
                else:
                    print("❌ Homing failed")

            elif choice == '7':
                # Refresh - just continue loop
                continue

            else:
                print("Invalid option")

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()


async def main():
    """Main function"""
    print("🔭 Telescope Slew Test Script")
    print("=" * 40)

    # Load configuration
    try:
        config_path = Path("telescope_config.yaml")
        if not config_path.exists():
            print("❌ Error: telescope_config.yaml not found in current directory")
            return

        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        print("✅ Configuration loaded successfully")

    except Exception as e:
        print(f"❌ Error loading config: {e}")
        return

    # Extract settings from config
    try:
        calibration_data = config
        comm_config = config.get('communication', {})

        device = comm_config.get('device', '/dev/ttyS0')
        baudrate = comm_config.get('baudrate', 9600)

        print(f"Device: {device}")
        print(f"Baudrate: {baudrate}")
        print(f"Observer Latitude: {config.get('observer_latitude', 'Unknown')}°")

    except Exception as e:
        print(f"❌ Error parsing config: {e}")
        return

    # Create and initialize telescope mount
    try:
        print("\n🔧 Initializing telescope mount...")
        mount = TelescopeMount(
            device=device,
            baudrate=baudrate,
            calibration_data=calibration_data
        )

        # Initialize the mount
        success = await mount.initialize()

        if not success:
            print("❌ Failed to initialize mount")
            return

        print("✅ Mount initialized successfully")

        # Wait for initial position update
        await asyncio.sleep(2)

        # Check if we want interactive mode
        if len(sys.argv) > 1 and sys.argv[1] == '--interactive':
            await interactive_mode(mount, config)
        else:
            # Just show current position and ask for one slew
            print_position_info(mount, config)

            print("\nSingle slew test mode (use --interactive for full control)")
            target_ha, target_dec = get_user_coordinates()

            if target_ha is not None:
                await perform_slew(mount, target_ha, target_dec)
                await asyncio.sleep(2)  # Let it settle
                print_position_info(mount, config)

        # Clean shutdown
        print("\n🔧 Shutting down...")
        await mount.shutdown()
        print("✅ Shutdown complete")

    except Exception as e:
        print(f"❌ Error with telescope mount: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    try:
        print("Usage:")
        print("  python telescope_slew_test.py           # Single slew test")
        print("  python telescope_slew_test.py --interactive  # Full interactive mode")
        print()

        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()