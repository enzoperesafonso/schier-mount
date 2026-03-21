import yaml
import logging
import os


class MountConfig:
    """
    Configuration management class for the Schier Mount of ROTSE-IIIc.
    Handles site location, encoder settings, movement limits, speeds,
    and parking positions.
    """
    def __init__(self):
        """
        Initializes the MountConfig with default parameters for the Schier Mount.

        Attributes:
            location (dict): Geographic coordinates (lat, lon, elevation) of the HESS site.
            encoder (dict): Encoder scaling factors (steps per degree), zero points, and tolerances.
            limits (dict): Minimum and maximum allowable movement ranges for RA and Dec.
            speeds (dict): Velocity settings for different movement modes (slew, fine, home, max).
            acceleration (dict): Acceleration profiles for RA and Dec axes.
            park (dict): Default parking coordinates.
            standby (dict): Default standby coordinates.

        Note:
            The configuration values are derived from the legacy Schierd config
            (Don Smith & E. Rykoff 2005) and are calibrated for the ROTSE-IIIc
            hardware specifications... Please no touchy-touchy without asking enzo!

        Returns:
            None
        """
        self.logger = logging.getLogger("MountConfig")

        # HESS Site
        self.location = {'latitude': -23.2716, 'longitude': 16.5, 'elevation': 1800}

        self.encoder = {

            'steps_per_deg_ra': 24382.0,
            'steps_per_deg_dec': 19395.0,

            'zeropt_ra': 0,
            'zeropt_dec': 0,
            'tolerance': 50
        }

        self.limits = {
            'ra_min': -185.0, 'ra_max': 0.0,
            'dec_min': 0.0, 'dec_max': 240.0
        }

        self.speeds = {
            'slew_ra': 5.0, 'slew_dec': 5.0,
            'fine_ra': 0.5, 'fine_dec': 0.5,
            'home_ra': 2.0, 'home_dec': 2.0,
            'max_ra': 30.0, 'max_dec':  30.0
        }

        self.acceleration = {
            'slew_ra': 25.0, 'slew_dec': 25.0,
        }

        self.park = {'ra': -95.0, 'dec': 90.0}

        self.standby = {'ra': -95.0, 'dec': 174.0}



    def update_zero_points(self, ra_counts, dec_counts):
        """
        Updates the encoder zero points in the current configuration instance.

        Args:
            ra_counts (int/float): The raw encoder count value for the Right Ascension zero point.
            dec_counts (int/float): The raw encoder count value for the Declination zero point.
        """
        self.encoder['zeropt_ra'] = int(ra_counts)
        self.encoder['zeropt_dec'] = int(dec_counts)
        self.logger.debug(f"Runtime Config Update: Zero Points set to RA={ra_counts}, Dec={dec_counts}")
