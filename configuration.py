import yaml
import logging
import os


class MountConfig:
    def __init__(self):
        """
        Args:
            config_source:
                - None: Use internal hardcoded defaults only.
                - dict: Apply dictionary overrides to defaults.
                - str: Load YAML file from path and apply to defaults.
        """
        self.logger = logging.getLogger("MountConfig")

        # Configuration file for custom Schier Mount of ROTSE-IIIc. These values are taken directly from the legacy Schierd config of the
        # original rotsed ocs system (Don Smith & E. Rykoff 2005) and should NOT be changed!

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
            'max_ra': 35.0, 'max_dec':  35.0
        }

        self.acceleration = {
            'slew_ra': 25.0, 'slew_dec': 25.0,
        }

        self.park = {'ra': -95.0, 'dec': 35.0}

        self.standby = {'ra': -95.0, 'dec': 90.0 + 23.2716}


    def update_zero_points(self, ra_counts, dec_counts):
        """Updates zero points in memory (runtime)."""
        self.encoder['zeropt_ra'] = int(ra_counts)
        self.encoder['zeropt_dec'] = int(dec_counts)
        self.logger.debug(f"Runtime Config Update: Zero Points set to RA={ra_counts}, Dec={dec_counts}")

