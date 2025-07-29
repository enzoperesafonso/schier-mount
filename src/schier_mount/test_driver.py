import asyncio
from schier_mount import SchierMount


async def main():
    # Example calibration data - should be loaded from a config file
    calibration_data = {
        'observer_latitude': 40.0,  # degrees
        'limits': {
            'ha_negative': -100000,
            'ha_positive': 100000,
            'dec_negative': -100000,
            'dec_positive': 100000,
        },
        'ranges': {
            'ha_encoder_range': 200000,
            'dec_encoder_range': 200000,
        },
        'ha_steps_per_degree': 1000,
        'dec_steps_per_degree': 1000,
        'limits_safety_factor': 0.05,
    }

    mount = SchierMount(device="/dev/ttyS0", calibration_data=calibration_data)

    try:
        # Connect to mount
        await mount.connect()

        # Home the mount
        await mount.home()

        # Move to specific coordinates (HA=1h, Dec=45°)
        await mount.move_to_ha_dec(1.0, -80)



    finally:
        # Ensure proper disconnection
        await mount.disconnect()


asyncio.run(main())