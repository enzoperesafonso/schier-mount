from coordinates import Coordinates
from state import MountStatus

test_calibration_data = {
    # overall calibration status
    'calibrated': True,

    'observer_latitude': -22.27,

    # date of calibration
    'calibration_date': '2024-01-15T20:30:00',

    # max slew speed for telescope goto
    'slew_speed': 5000,

    'ha_steps_per_degree': 20000000 / 180,
    'dec_steps_per_degree': 20000000 / 233,

    # number of steps we define as a safety buffer around all encoder hardstops
    'limits_safety_buffer': 100,

    # encoder limits (raw encoder counts)
    'limits': {
        # RA axis encoder limits (±6 hours of movement)
        'ha_negative':  -1000000,  # Encoder count at HA = -6 hours
        'ha_positive': 1000000,  # Encoder count at HA = +6 hours

        # Dec axis encoder limits
        'dec_negative': -1000000,  # Encoder count at minimum declination
        'dec_positive': 1000000,  # Encoder count at maximum declination
    },

    # calculated ranges from limits
    'ranges': {
        'ha_encoder_range': 2000000,  # ra_positive - ra_negative
        'dec_encoder_range': 2000000,  # dec_positive - dec_negative
    },


    'ra_limits': {
        # HARDCODED DO NOT TOUCH: mount maps from -6 to +6 where the + is pointing direction
        'positive_hours': -6,  # Maximum mechanical declination
        'negative_hours': 6,  # Minimum mechanical declination
        'dec_angular_range': 235.0,  # Total angular range (122 - (-113))
    },


}