import yaml
import logging
import os


class MountConfig:
    def __init__(self, config_source=None):
        """
        Args:
            config_source:
                - None: Use internal hardcoded defaults only.
                - dict: Apply dictionary overrides to defaults.
                - str: Load YAML file from path and apply to defaults.
        """
        self.logger = logging.getLogger("MountConfig")
        self.config_file = None

        # --- 1. HARDCODED DEFAULTS ---
        self.location = {'latitude': -33.9, 'longitude': 18.4, 'elevation': 100}

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
            'slew_ra': 25.0, 'slew_dec': 25.0,
            'home_ra': 2.0, 'home_dec': 2.0
        }

        self.park = {'ra': -95.0, 'dec': 35.0}

        # T-Point Model (Empty by default)
        self.pointing_model = {
            'IH': 0.0, 'ID': 0.0, 'NP': 0.0,
            'CH': 0.0, 'ME': 0.0, 'MA': 0.0, 'FO': 0.0
        }

        # --- 2. LOAD CONFIGURATION ---
        if isinstance(config_source, dict):
            self.logger.info("Configuration loaded from Dictionary.")
            self._apply_dict(config_source)

        elif isinstance(config_source, str):
            self.config_file = config_source
            self.load_from_file()

        else:
            self.logger.info("No config source provided. Using Hardcoded Defaults.")

    def _apply_dict(self, data):
        """Merges a dictionary into the existing config."""
        try:
            # --- 1. Encoder Settings ---
            if 'encoder_tolerance' in data:
                self.encoder['tolerance'] = data['encoder_tolerance']

            if 'steps_per_degree' in data:
                spd = data['steps_per_degree']
                self.encoder['steps_per_deg_ra'] = spd.get('ra', self.encoder['steps_per_deg_ra'])
                self.encoder['steps_per_deg_dec'] = spd.get('dec', self.encoder['steps_per_deg_dec'])

            # --- 2. Speeds ---
            # Map 'max_velocity' (YAML) -> 'slew_*' (Internal)
            if 'max_velocity' in data:
                mv = data['max_velocity']
                self.speeds['slew_ra'] = mv.get('ra', self.speeds['slew_ra'])
                self.speeds['slew_dec'] = mv.get('dec', self.speeds['slew_dec'])

            # Map 'home_velocity' (YAML) -> 'home_*' (Internal)
            if 'home_velocity' in data:
                hv = data['home_velocity']
                self.speeds['home_ra'] = hv.get('ra', self.speeds['home_ra'])
                self.speeds['home_dec'] = hv.get('dec', self.speeds['home_dec'])

            # --- 3. Park / Stow Position ---
            if 'stow_position' in data:
                stow = data['stow_position']
                self.park['ra'] = stow.get('ra', self.park['ra'])
                self.park['dec'] = stow.get('dec', self.park['dec'])

            # --- 4. Axis Limits ---
            # Handles parsing strings like "(-185.0 , 0.0)"
            if 'axis_range' in data:
                ar = data['axis_range']

                def parse_tuple_str(val):
                    """Parses '(min, max)' string or returns list/tuple directly."""
                    if isinstance(val, str):
                        # Remove parens and split by comma
                        clean = val.replace('(', '').replace(')', '')
                        parts = clean.split(',')
                        return float(parts[0]), float(parts[1])
                    return val[0], val[1]  # Assume list/tuple

                if 'ra' in ar:
                    self.limits['ra_min'], self.limits['ra_max'] = parse_tuple_str(ar['ra'])

                if 'dec' in ar:
                    self.limits['dec_min'], self.limits['dec_max'] = parse_tuple_str(ar['dec'])

            # --- 5. Runtime Injection (Zero Points & Model) ---
            if 'zero_points' in data:
                zp = data['zero_points']
                self.encoder['zeropt_ra'] = zp.get('ra', self.encoder['zeropt_ra'])
                self.encoder['zeropt_dec'] = zp.get('dec', self.encoder['zeropt_dec'])

            if 'pointing_model' in data:
                self.pointing_model.update(data['pointing_model'])

        except Exception as e:
            self.logger.error(f"Config parsing error: {e}")

    def load_from_file(self):
        if not self.config_file or not os.path.exists(self.config_file):
            self.logger.error(f"Config file '{self.config_file}' not found.")
            return

        try:
            with open(self.config_file, 'r') as f:
                data = yaml.safe_load(f)
            self._apply_dict(data)
            self.logger.info(f"Loaded config from {self.config_file}")
        except Exception as e:
            self.logger.error(f"Failed to read config file: {e}")

    def update_zero_points(self, ra_counts, dec_counts):
        """Updates zero points in memory (runtime)."""
        self.encoder['zeropt_ra'] = int(ra_counts)
        self.encoder['zeropt_dec'] = int(dec_counts)
        self.logger.info(f"Runtime Config Update: Zero Points set to RA={ra_counts}, Dec={dec_counts}")