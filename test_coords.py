import unittest
import logging
import sys
from astropy.time import Time
from astropy import units as u

# Import your modules
from configuration import MountConfig
from coordinates import CoordinateEngine


class TestCoordinates(unittest.TestCase):
    def setUp(self):
        # 1. Setup a clean configuration for testing
        # We use a dictionary to force known values (ignoring conf.yaml for the test)
        self.test_conf = {
            'encoder_tolerance': 100,
            'steps_per_degree': {'ra': 10000.0, 'dec': 10000.0},  # Simple numbers for easy math
            'zero_points': {'ra': 0, 'dec': 0},
            'pointing_model': {
                'IH': 0.0, 'ID': 0.0, 'NP': 0.0,
                'CH': 0.0, 'ME': 0.0, 'MA': 0.0, 'FO': 0.0
            }
        }
        self.cfg = MountConfig(self.test_conf)

        # Force location to Cape Town (South)
        self.cfg.location['latitude'] = -33.9
        self.cfg.location['longitude'] = 18.4

        self.coords = CoordinateEngine(self.cfg)

        # Fixed time for consistent results
        self.now = Time('2025-01-01 22:00:00')

    def test_round_trip_accuracy(self):
        """
        Verify that RA/Dec -> Encoder -> RA/Dec returns the same coordinates.
        (With T-Point disabled)
        """
        print("\n--- Test: Round Trip Accuracy ---")

        # Define a target (e.g., Sirius)
        ra_in = 10.287  # deg
        dec_in = -16.716  # deg

        # 1. Forward
        enc_ra, enc_dec = self.coords.radec_to_encoder(ra_in, dec_in, obstime=self.now)

        # 2. Backward
        ra_out, dec_out = self.coords.encoder_to_radec(enc_ra, enc_dec, obstime=self.now)

        print(f"Input:  RA={ra_in:.4f}, Dec={dec_in:.4f}")
        print(f"Output: RA={ra_out:.4f}, Dec={dec_out:.4f}")

        # Assert equality (allow small float error)
        self.assertAlmostEqual(ra_in, ra_out, places=3)
        self.assertAlmostEqual(dec_in, dec_out, places=3)

    def test_southern_hemisphere_flip(self):
        """
        Verify that Southern latitudes reverse the encoder direction.
        """
        print("\n--- Test: Southern Hemisphere Flip ---")

        target_ra = 10.0
        target_dec = -45.0

        # Case A: SOUTH (-33.9)
        self.cfg.location['latitude'] = -33.9
        enc_ra_south, enc_dec_south = self.coords.radec_to_encoder(target_ra, target_dec, obstime=self.now)

        # Case B: NORTH (+33.9)
        self.cfg.location['latitude'] = 33.9
        # We must re-init coords to update the internal EarthLocation
        coords_north = CoordinateEngine(self.cfg)
        enc_ra_north, enc_dec_north = coords_north.radec_to_encoder(target_ra, target_dec, obstime=self.now)

        print(f"South Encoder RA: {enc_ra_south}")
        print(f"North Encoder RA: {enc_ra_north}")

        # In Schier code, South = -1 * North (approximately, ignoring T-Point differences due to Lat)
        # Note: LST is same for same longitude, so HA is same.
        # The logic `if lat < 0: ha *= -1` implies the signs should be opposite.
        self.assertNotEqual(enc_ra_south, enc_ra_north)

        # Basic check: Are they roughly opposite?
        # (Precise check is hard due to T-Point 'TF' term depending on Lat, but signs should flip)
        self.assertTrue((enc_ra_south > 0 and enc_ra_north < 0) or (enc_ra_south < 0 and enc_ra_north > 0),
                        "Encoder signs did not flip between hemispheres!")



if __name__ == '__main__':
    # Setup logging to see output
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    unittest.main()