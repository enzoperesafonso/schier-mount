#!/usr/bin/env python3
"""
Simple script to load telescope configuration and get current HA/Dec position
"""

import asyncio
import yaml
import logging
from pathlib import Path

# Import your telescope mount classes
try:
    from schier_mount import TelescopeMount
except ImportError as e:
    print(f"Error importing telescope_mount: {e}")
    print("Make sure telescope_mount.py is in the current directory")
    exit(1)

# Simple logging setup
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def main():
    """Load telescope and get current position"""

    # Load configuration
    try:
        config_path = Path("telescope_config.yaml")
        if not config_path.exists():
            print("Error: telescope_config.yaml not found in current directory")
            return

        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        print("✓ Configuration loaded successfully")

    except Exception as e:
        print(f"Error loading config: {e}")
        return

    # Extract settings from config
    try:
        calibration_data = config.get('calibration', {})
        comm_config = config.get('communication', {})

        device = comm_config.get('device', '/dev/ttyS0')
        baudrate = comm_config.get('baudrate', 9600)

        print(f"Device: {device}")
        print(f"Baudrate: {baudrate}")

    except Exception as e:
        print(f"Error parsing config: {e}")
        return

    # Create and initialize telescope mount
    try:
        print("\nInitializing telescope mount...")
        mount = TelescopeMount(
            device=device,
            baudrate=baudrate,
            calibration_data=calibration_data
        )

        # Initialize the mount
        success = await mount.initialize()

        if not success:
            print("Failed to initialize mount")
            return

        print("✓ Mount initialized successfully")

        # Wait a moment for position updates
        await asyncio.sleep(2)

        # Get current position
        current_ha, current_dec = mount.get_current_position()

        if current_ha is not None and current_dec is not None:
            print(f"\nCurrent Position:")
            print(f"  Hour Angle: {current_ha:.4f} hours ({current_ha * 15:.3f}°)")
            print(f"  Declination: {current_dec:.4f}°")

            # Convert HA to more readable format
            ha_hours = int(current_ha)
            ha_minutes = int((current_ha - ha_hours) * 60)
            ha_seconds = ((current_ha - ha_hours) * 60 - ha_minutes) * 60

            dec_degrees = int(current_dec)
            dec_arcmin = int(abs(current_dec - dec_degrees) * 60)
            dec_arcsec = (abs(current_dec - dec_degrees) * 60 - dec_arcmin) * 60

            print(f"\nFormatted Position:")
            print(f"  HA: {ha_hours:+03d}h {ha_minutes:02d}m {ha_seconds:04.1f}s")
            print(f"  Dec: {dec_degrees:+03d}° {dec_arcmin:02d}' {dec_arcsec:04.1f}\"")

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
            print(f"  Raw Encoders: RA={status.ra_encoder}, Dec={status.dec_encoder}")

        # Clean shutdown
        print("\nShutting down...")
        await mount.shutdown()
        print("✓ Shutdown complete")

    except Exception as e:
        print(f"Error with telescope mount: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Unexpected error: {e}")
        import traceback

        traceback.print_exc()