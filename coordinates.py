import logging
from astropy.coordinates import SkyCoord, EarthLocation, FK5
from astropy.time import Time
from astropy import units as u
import numpy as np


class MountCoordinates:
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger("SchierMount.Coords")

        self.location = EarthLocation(
            lon=self.config.location['longitude'] * u.deg,
            lat=self.config.location['latitude'] * u.deg,
            height=self.config.location['elevation'] * u.m
        )
        self.j2000_frame = FK5(equinox='J2000')

    def get_lst(self):
        now = Time.now()
        return now.sidereal_time('mean', longitude=self.location.lon).deg

    def hadec_to_enc(self, ha_deg: float, dec_deg: float) -> tuple[int, int]:
        """
        Converts Hour Angle (HA) and Declination (Dec) to encoder counts.
        Handles below-pole pointing logic asnd mechanical limits.
        
        Mechanical mapping:
        - RA center (-92.5) = HA 0
        - Dec center (120) = SCP (Dec -90)
        - RA Range: [-185, 0]
        - Dec Range: [0, 240]
        """
        # Normalize HA to [-180, 180]
        ha = ((ha_deg + 180) % 360) - 180

        # Try Normal Mode
        # mech_ha = HA - 92.5
        # mech_dec = Dec + 210 (since -90 -> 120, offset is 210)
        mech_ha_normal = ha - 92.5
        mech_dec_normal = dec_deg + 210

        # Try Below-Pole Mode
        # ha_flipped = HA + 180
        # mech_dec = 120 - (Dec + 90) = 30 - Dec
        ha_flipped = ((ha + 180 + 180) % 360) - 180
        mech_ha_below = ha_flipped - 92.5
        mech_dec_below = 30 - dec_deg

        # Check limits for Normal Mode
        ra_lim = (self.config.limits['ra_min'], self.config.limits['ra_max'])
        dec_lim = (self.config.limits['dec_min'], self.config.limits['dec_max'])

        normal_ok = (ra_lim[0] <= mech_ha_normal <= ra_lim[1]) and \
                    (dec_lim[0] <= mech_dec_normal <= dec_lim[1])

        below_ok = (ra_lim[0] <= mech_ha_below <= ra_lim[1]) and \
                   (dec_lim[0] <= mech_dec_below <= dec_lim[1])

        if normal_ok:
            mech_ha, mech_dec = mech_ha_normal, mech_dec_normal
            mode = "Normal"
        elif below_ok:
            mech_ha, mech_dec = mech_ha_below, mech_dec_below
            mode = "Below-Pole"
        else:
            # Fallback to normal but it will likely trigger a safety error in comm.py
            mech_ha, mech_dec = mech_ha_normal, mech_dec_normal
            mode = "INVALID (Out of limits)"
            self.logger.warning(f"Target HA={ha_deg:.2f}, Dec={dec_deg:.2f} is out of mechanical limits!")

        self.logger.debug(f"Target: HA={ha:.2f}, Dec={dec_deg:.2f} -> Mech RA={mech_ha:.2f}, Dec={mech_dec:.2f} ({mode})")

        enc_ra = int(mech_ha * self.config.encoder['steps_per_deg_ra'] + self.config.encoder['zeropt_ra'])
        enc_dec = int(mech_dec * self.config.encoder['steps_per_deg_dec'] + self.config.encoder['zeropt_dec'])

        return enc_ra, enc_dec

    def enc_to_hadec(self, ra_enc: int, dec_enc: int) -> tuple[float, float]:
        """
        Converts encoder counts back to HA and Dec degrees.
        """
        # Convert encoder counts back to mechanical degrees
        mech_ha = (ra_enc - self.config.encoder['zeropt_ra']) / self.config.encoder['steps_per_deg_ra']
        mech_dec = (dec_enc - self.config.encoder['zeropt_dec']) / self.config.encoder['steps_per_deg_dec']

        # Determine if we are in Normal or Below-Pole mode based on mech_dec
        # Center of Dec range (120) is the pole.
        # mech_dec > 120 is looking towards the equator in Normal mode.
        # mech_dec < 120 is looking "under" the pole (Below-Pole).
        
        if mech_dec >= 120:
            # Normal Mode: mech_dec = Dec + 210 => Dec = mech_dec - 210
            dec_deg = mech_dec - 210
            # mech_ha = HA - 92.5 => HA = mech_ha + 92.5
            ha_deg = mech_ha + 92.5
        else:
            # Below-Pole Mode: mech_dec = 30 - Dec => Dec = 30 - mech_dec
            dec_deg = 30 - mech_dec
            # mech_ha = HA_flipped - 92.5 => HA_flipped = mech_ha + 92.5
            ha_flipped = mech_ha + 92.5
            # HA = HA_flipped + 180
            ha_deg = ha_flipped + 180

        return ha_deg % 360, dec_deg
