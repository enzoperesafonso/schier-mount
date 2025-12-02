import yaml
import logging
import os

# Configuration file for custom Schier Mount of ROTSE-IIIc. These values are taken directly from the legacy Schierd config of the
# original rotsed ocs system (Don Smith & E. Rykoff 2005) and should NOT be changed.
# Enzo Peres Afonso 2025

class MountConfig:
    def __init__(self, config_source="conf.yaml"):
        """
        Args:
            config_source: Can be a file path (str) OR a dictionary of values.
        """
        self.logger = logging.getLogger("MountConfig")

        # --- 1. Set Hardcoded Defaults ---
        # These prevent crashes if the config (file or dict) is partial
        self.location = {
            'latitude': -33.9,
            'longitude': 18.4,
            'elevation': 100
        }
        self.encoder = {
            'steps_per_deg_ra': 24382.0,
            'steps_per_deg_dec': 19395.0,
            'zeropt_ra': 0,
            'zeropt_dec': 0,
            'tolerance': 100
        }
        self.limits = {
            'ra_min': -185.0, 'ra_max': 0.0,
            'dec_min': 0.0, 'dec_max': 240.0
        }
        self.speeds = {
            'slew_ra': 35.0, 'slew_dec': 35.0,
            'home_ra': 2.0, 'home_dec': 2.0
        }
        self.park_position = {'ra': -95.0, 'dec': 35.0}

        self.pointing_model = {
            'IH': 0.0, 'ID': 0.0, 'NP': 0.0,
            'CH': 0.0, 'ME': 0.0, 'MA': 0.0, 'FO': 0.0
        }

        # --- 2. Load Configuration ---
        if isinstance(config_source, dict):
            # Direct Dictionary Injection
            self.logger.info("Loading configuration from dictionary.")
            self._apply_dict(config_source)
            self.config_file = None  # No file associated

        elif isinstance(config_source, str):
            # File Path Load
            self.config_file = config_source
            self.load_from_file()

    def _apply_dict(self, data):
        """
        Internal method to map a raw dictionary (YAML structure)
        to class attributes.
        """
        try:
            # Encoder
            if 'encoder_tolerance' in data:
                self.encoder['tolerance'] = data['encoder_tolerance']

            if 'steps_per_degree' in data:
                # Support both flat and nested dict access safely
                spd = data['steps_per_degree']
                self.encoder['steps_per_deg_ra'] = spd.get('ra', self.encoder['steps_per_deg_ra'])
                self.encoder['steps_per_deg_dec'] = spd.get('dec', self.encoder['steps_per_deg_dec'])

            # Speeds
            if 'max_velocity' in data:
                self.speeds['slew_ra'] = data['max_velocity'].get('ra', 35.0)
                self.speeds['slew_dec'] = data['max_velocity'].get('dec', 35.0)

            if 'home_velocity' in data:
                self.speeds['home_ra'] = data['home_velocity'].get('ra', 2.0)
                self.speeds['home_dec'] = data['home_velocity'].get('dec', 2.0)

            # Limits (Parsing the tuple string format if necessary)
            if 'axis_range' in data:
                ra_range = data['axis_range'].get('ra')
                dec_range = data['axis_range'].get('dec')

                # Helper to parse "(-185.0, 0.0)" string or list/tuple
                def parse_range(val):
                    if isinstance(val, str):
                        return map(float, val.strip('()').split(','))
                    return val  # Assume it's already a list/tuple

                if ra_range:
                    self.limits['ra_min'], self.limits['ra_max'] = parse_range(ra_range)
                if dec_range:
                    self.limits['dec_min'], self.limits['dec_max'] = parse_range(dec_range)

            # Park
            if 'stow_position' in data:
                self.park_position.update(data['stow_position'])

            # Pointing Model
            if 'pointing_model' in data:
                self.pointing_model.update(data['pointing_model'])

        except Exception as e:
            self.logger.error(f"Error parsing configuration data: {e}")

    def load_from_file(self):
        """Loads YAML file and passes it to _apply_dict."""
        if not self.config_file or not os.path.exists(self.config_file):
            self.logger.warning("Config file not found. Using defaults.")
            return

        try:
            with open(self.config_file, 'r') as f:
                data = yaml.safe_load(f)
            self._apply_dict(data)
            self.logger.info(f"Loaded config from {self.config_file}")
        except Exception as e:
            self.logger.error(f"Failed to load config file: {e}")