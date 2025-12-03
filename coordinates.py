import numpy as np
from astropy.coordinates import SkyCoord, EarthLocation, FK5
from astropy.time import Time
from astropy import units as u
import logging


class CoordinateEngine:
    def __init__(self, config):
        """
        Args:
            config: An instance of MountConfig (from configuration.py)
        """
        self.cfg = config
        self.logger = logging.getLogger("Coords")

        # Astropy Site Location
        self.location = EarthLocation(
            lat=self.cfg.location['latitude'] * u.deg,
            lon=self.cfg.location['longitude'] * u.deg,
            height=self.cfg.location['elevation'] * u.m
        )

    def radec_to_encoder(self, ra_deg, dec_deg, obstime=None):
        """
        Converts J2000 Sky Coordinates to Mount Encoder Steps.
        Replicates logic from: coord2enc.c and apply_model.c
        """
        if obstime is None:
            obstime = Time.now()

        # 1. Precession & Nutation (J2000 -> Current Epoch)
        # coord2enc.c uses slaPreces
        c_j2000 = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame='icrs')
        c_current = c_j2000.transform_to(FK5(equinox=obstime))

        # 2. Calculate Hour Angle (HA)
        # HA = LST - RA
        lst = obstime.sidereal_time('mean', longitude=self.location.lon)
        ha = (lst - c_current.ra).wrap_at(180 * u.deg)

        ha_deg = ha.deg
        dec_deg = c_current.dec.deg

        # apply pointing model at some point to get corrections in ha dec ..
        d_ha, d_dec = 0,0 #self._apply_tpoint(ha_deg, dec_deg)

        corrected_ha = ha_deg + d_ha
        corrected_dec = dec_deg + d_dec

        # 4. Southern Hemisphere Flip
        # This reverses the motor direction for the Southern Hemisphere
        if self.cfg.location['latitude'] < 0.0:
            corrected_ha *= -1.0
            corrected_dec *= -1.0

        # 5. Convert to Encoder Steps
        # Enc = (Degrees * Steps/Deg) + Zero_Point
        steps_ra = self.cfg.encoder['steps_per_deg_ra']
        steps_dec = self.cfg.encoder['steps_per_deg_dec']
        zero_ra = self.cfg.encoder['zeropt_ra']
        zero_dec = self.cfg.encoder['zeropt_dec']

        enc_ra = int((corrected_ha * steps_ra) + zero_ra)
        enc_dec = int((corrected_dec * steps_dec) + zero_dec)

        return enc_ra, enc_dec

    def encoder_to_radec(self, enc_ra, enc_dec, obstime=None):
        """
        Converts Raw Encoder Steps to J2000 Sky Coordinates.
        Replicates logic from: enc2radec.c
        """
        if obstime is None:
            obstime = Time.now()

        steps_ra = self.cfg.encoder['steps_per_deg_ra']
        steps_dec = self.cfg.encoder['steps_per_deg_dec']
        zero_ra = self.cfg.encoder['zeropt_ra']
        zero_dec = self.cfg.encoder['zeropt_dec']

        # 1. Convert Steps to Raw Degrees (Remove Zero Point)
        raw_ha = (enc_ra - zero_ra) / steps_ra
        raw_dec = (enc_dec - zero_dec) / steps_dec

        # 2. Southern Hemisphere Flip (Reverse)
        # enc2radec.c
        if self.cfg.location['latitude'] < 0.0:
            raw_ha *= -1.0
            raw_dec *= -1.0

        # 3. Inverse T-Point (Simplified)
        # The C code 'enc2radec.c' DOES NOT apply the inverse model. 
        # It calculates purely geometric RA/Dec. We will do the same here.
        # If you need higher precision reporting, we can subtract the model terms.

        # 4. Calculate RA from HA
        # RA = LST - HA
        lst = obstime.sidereal_time('mean', longitude=self.location.lon)
        ra_val = (lst.deg - raw_ha) % 360.0  # Normalize 0-360

        # 5. Transform Current Epoch -> J2000
        # Create coord at current equinox
        c_current = SkyCoord(ra=ra_val * u.deg, dec=raw_dec * u.deg, frame=FK5(equinox=obstime))
        c_j2000 = c_current.transform_to('icrs')

        return c_j2000.ra.deg, c_j2000.dec.deg

    def _apply_tpoint(self, ha_deg, dec_deg):
        """
        Calculates mechanical corrections (in degrees).
        Ported from apply_model.c
        """
        terms = self.cfg.pointing_model

        # Convert to radians for trig functions
        ha_rad = np.radians(ha_deg)
        dec_rad = np.radians(dec_deg)
        lat_rad = np.radians(self.cfg.location['latitude'])

        sin_ha = np.sin(ha_rad)
        cos_ha = np.cos(ha_rad)
        tan_dec = np.tan(dec_rad)
        sec_dec = 1.0 / np.cos(dec_rad)

        d_ha = 0.0
        d_dec = 0.0

        # --- Standard T-Point Terms ---

        # IH: Index Error HA
        d_ha += terms.get('IH', 0.0)

        # ID: Index Error Dec
        d_dec += terms.get('ID', 0.0)

        # NP: Non-perpendicularity (HA/Dec axis)
        if terms.get('NP'):
            d_ha += terms['NP'] * tan_dec

        # CH: Collimation Error (Optical/Dec axis)
        if terms.get('CH'):
            d_ha += terms['CH'] * sec_dec

        # ME: Polar Misalignment (Elevation)
        if terms.get('ME'):
            d_ha += terms['ME'] * sin_ha * tan_dec
            d_dec += terms['ME'] * cos_ha

        # MA: Polar Misalignment (Azimuth)
        if terms.get('MA'):
            d_ha -= terms['MA'] * cos_ha * tan_dec
            d_dec += terms['MA'] * sin_ha

        # FO: Tube Flexure
        if terms.get('FO'):
            d_dec += terms['FO'] * cos_ha

        # TF: Tube Flexure (Complex) - Included in C code
        if terms.get('TF'):
            d_ha += terms['TF'] * np.cos(lat_rad) * sin_ha * sec_dec
            d_dec += terms['TF'] * (np.cos(lat_rad) * cos_ha * np.sin(dec_rad) - np.sin(lat_rad) * np.cos(dec_rad))

        # Scaling: C code applies 's2d' (1/3600) to terms. 
        # If your YAML values are in Arcseconds, uncomment this:
        # d_ha /= 3600.0
        # d_dec /= 3600.0

        # Note: apply_model.c handles "Sign" logic based on pier side. 
        # For a standard German Equatorial Mount in South, the flip above handles the bulk.

        return d_ha, d_dec