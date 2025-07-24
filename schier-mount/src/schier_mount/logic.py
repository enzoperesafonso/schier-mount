from utils.mount_coordinate_transformer import MountCoordinateTransformer

test_calibration_data = {
    # overall calibration status
    'calibrated': True,

    # date of calibration
    'calibration_date': '2024-01-15T20:30:00',

    # sidereal tracking speed in encoder steps / second
    'sidereal_rate': 100,

    # max slew speed for telescope goto
    'slew_speed': 5000,

    # number of steps we define as a safety buffer around all encoder hardstops
    'limits_safety_buffer': 100,

    # encoder limits (raw encoder counts)
    'limits': {
        # RA axis encoder limits (±6 hours of movement)
        'ra_negative':  -2349433,  # Encoder count at HA = -6 hours
        'ra_positive': 2138930,  # Encoder count at HA = +6 hours

        # Dec axis encoder limits
        'dec_negative': -559784,  # Encoder count at minimum declination
        'dec_positive': 3978578,  # Encoder count at maximum declination
    },

    # calculated ranges from limits
    'ranges': {
        'ra_encoder_range': 4488363,  # ra_positive - ra_negative
        'dec_encoder_range': 4538362,  # dec_positive - dec_negative
    },

    # declination mechanical limits in degrees
    'dec_limits': {
        # HARDCODED DO NOT TOUCH: mount maps from +122 to -113 where 122 is the + pointing direction
        'positive_degrees': 122.0,  # Maximum mechanical declination
        'negative_degrees': -113.0,  # Minimum mechanical declination
        'dec_angular_range': 235.0,  # Total angular range (122 - (-113))
    },

    'ra_limits': {
        # HARDCODED DO NOT TOUCH: mount maps from -6 to +6 where the + is pointing direction
        'positive_hours': -6,  # Maximum mechanical declination
        'negative_hours': 6,  # Minimum mechanical declination
        'dec_angular_range': 235.0,  # Total angular range (122 - (-113))
    },


}

coord = MountCoordinateTransformer(test_calibration_data)

import asyncio

print(asyncio.run(coord.astro_ha_dec_to_encoder_steps(0, -80)))


