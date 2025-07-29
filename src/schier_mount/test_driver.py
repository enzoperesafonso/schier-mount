import asyncio
from mount_control import MountDriver

test_calibration_data = {
    # overall calibration status
    'calibrated': True,

    'observer_latitude': -22.27,

    # date of calibration
    'calibration_date': '2024-01-15T20:30:00',

    # max slew speed for telescope goto
    'slew_speed': 5000,

    'ha_steps_per_degree': 4495610 / 180,
    'dec_steps_per_degree': 4535120 / 400,

    # number of steps we define as a safety buffer around all encoder hardstops
    'limits_safety_factor': 0.005,

    # encoder limits (raw encoder counts)
    'limits': {
        # RA axis encoder limits (±6 hours of movement)
        'ha_negative':  -2261556,  # Encoder count at HA = -6 hours
        'ha_positive': 2234054,  # Encoder count at HA = +6 hours

        # Dec axis encoder limits
        'dec_negative': -1533837,  # Encoder count at minimum declination
        'dec_positive': 3001283,  # Encoder count at maximum declination
    },

    # calculated ranges from limits
    'ranges': {
        'ha_encoder_range': 4495610,  # ra_positive - ra_negative
        'dec_encoder_range': 4535120,  # dec_positive - dec_negative
    },


    'ra_limits': {
        # HARDCODED DO NOT TOUCH: mount maps from -6 to +6 where the + is pointing direction
        'positive_hours': -6,  # Maximum mechanical declination
        'negative_hours': 6,  # Minimum mechanical declination
        'dec_angular_range': 235.0,  # Total angular range (122 - (-113))
    },


}


async def main():
    # Initialize with your calibration data
    mount = MountDriver(device="/dev/ttyS0", calibration_data=test_calibration_data)

    try:
        # Connect and home the mount
        await mount.connect()

        # Add progress callback
        def progress_callback(progress):
            print(f"Slew progress: {progress.progress_percent:.1f}% - State: {progress.state.value}")

        mount.add_progress_callback(progress_callback)

        # Slew to a target (HA in hours, Dec in degrees)
        await mount.goto_ha_dec(0, -90)


    except Exception as e:
        print(f"Mount error: {e}")
    finally:
        await mount.disconnect()


asyncio.run(main())