import logging
from astropy.coordinates import SkyCoord, EarthLocation, FK5
from astropy import units as u

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
    def get_lst(self) -> float:
        """
        Calculates the current Local Sidereal Time (LST) in degrees.
        """
        from astropy.time import Time
        now = Time.now()
        lst = now.sidereal_time('mean', longitude=self.location.lon)
        return lst.deg

    def ra_to_ha(self, ra_deg: float) -> float:
        """
        Converts Right Ascension (RA) to Hour Angle (HA).
        HA = LST - RA
        """
        lst = self.get_lst()
        ha = (lst - ra_deg + 360) % 360
        if ha > 180:
            ha -= 360
        return ha

    def ha_to_ra(self, ha_deg: float) -> float:
        """
        Converts Hour Angle (HA) to Right Ascension (RA).
        RA = LST - HA
        """
        lst = self.get_lst()
        ra = (lst - ha_deg + 360) % 360
        return ra

    def radec_to_enc(self, ra_deg: float, dec_deg: float) -> tuple[int, int]:
        """
        Converts Right Ascension (RA) and Declination (Dec) to encoder counts.
        """
        ha_deg = self.ra_to_ha(ra_deg)
        return self.hadec_to_enc(ha_deg, dec_deg)

    def enc_to_radec(self, ra_enc: int, dec_enc: int) -> tuple[float, float]:
        """
        Converts encoder counts back to RA and Dec degrees.
        """
        ha_deg, dec_deg = self.enc_to_hadec(ra_enc, dec_enc)
        ra_deg = self.ha_to_ra(ha_deg)
        return ra_deg, dec_deg

    def hadec_to_enc(self, ha_deg: float, dec_deg: float) -> tuple[int, int]:
        """
        Converts Hour Angle (HA) and Declination (Dec) to encoder counts.
        Handles meridian flip (below-pole pointing) and mechanical limits.
        
        Mechanical mapping:
        - RA center (-92.5) = HA 0
        - Dec center (120) = SCP (Dec -90)
        - RA Range: [-185, 0]
        - Dec Range: [0, 240]
        
        The "flip" identity: (HA, Dec) == (HA + 180, 180 - Dec)
        For this mount, flipping is handled by choosing the mechanical branch 
        that stays within [0, 240] for Dec and [-185, 0] for RA.
        """
        # Normalize HA to [-180, 180]
        ha = ((ha_deg + 180) % 360) - 180
        pole_offset = self.config.encoder.get('pole_offset', 0.0)

        # Try Normal Mode
        # mech_ha = -HA - 92.5
        # mech_dec = Dec + 210
        mech_ha_normal = -ha - 92.5
        mech_dec_normal = dec_deg + 210 + pole_offset

        # Try Below-Pole (Flipped) Mode
        # ha_flipped = -(HA + 180)
        ha_flipped = (ha % 360) - 180
        mech_ha_below = -ha_flipped - 92.5
        mech_dec_below = 30 - dec_deg + pole_offset

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

    def is_below_pole(self, ra_enc: int, dec_enc: int) -> bool:
        """
        Returns True if the mount is in Below-Pole (flipped) orientation.
        """
        pole_offset = self.config.encoder.get('pole_offset', 0.0)
        mech_dec = (dec_enc - self.config.encoder['zeropt_dec']) / self.config.encoder['steps_per_deg_dec'] - pole_offset
        return mech_dec < 120

    def enc_to_hadec(self, ra_enc: int, dec_enc: int) -> tuple[float, float]:
        """
        Converts encoder counts back to HA and Dec degrees.
        """
        pole_offset = self.config.encoder.get('pole_offset', 0.0)
        
        # Convert encoder counts back to mechanical degrees
        mech_ha = (ra_enc - self.config.encoder['zeropt_ra']) / self.config.encoder['steps_per_deg_ra']
        # Apply pole offset to get back to the theoretical mechanical frame
        mech_dec = (dec_enc - self.config.encoder['zeropt_dec']) / self.config.encoder['steps_per_deg_dec'] - pole_offset

        # Determine if we are in Normal or Below-Pole mode based on mech_dec
        # Center of Dec range (120) is the pole.
        
        if mech_dec >= 120:
            # Normal Mode: mech_dec = Dec + 210 => Dec = mech_dec - 210
            dec_deg = mech_dec - 210
            # mech_ha = -HA - 92.5 => HA = -(mech_ha + 92.5)
            ha_deg = -(mech_ha + 92.5)
        else:
            # Below-Pole Mode: mech_dec = 30 - Dec => Dec = 30 - mech_dec
            dec_deg = 30 - mech_dec
            # mech_ha = -HA_flipped - 92.5 => HA_flipped = -(mech_ha + 92.5)
            ha_flipped = -(mech_ha + 92.5)
            # HA = HA_flipped + 180 (or HA_flipped - 180, normalize it)
            ha_deg = ha_flipped + 180

        return ha_deg % 360, dec_deg
