import logging
import math
from astropy.coordinates import SkyCoord, EarthLocation, FK5, AltAz
from astropy.time import Time
from astropy import units as u
from configuration import MountConfig

class MountCoordinates:
    """
    Robust Coordinate Conversion Module for SchierMount.
    Reads zero points dynamically from config to support runtime calibration.
    """

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger("SchierMount.Coords")

        # EarthLocation is static, so we can cache it
        self.location = EarthLocation(
            lon=self.config.longitude * u.deg,
            lat=self.config.latitude * u.deg,
            height=self.config.elevation * u.m
        )

        # Cache static scale factors (these likely won't change during a night)
        self.steps_per_deg_ra = self.config.encoder['steps_per_deg_ra']
        self.steps_per_deg_dec = self.config.encoder['steps_per_deg_dec']

    def radec_to_enc(self, ra_deg: float, dec_deg: float, time_offset_sec: float = 0.0) -> tuple[int, int]:
        """Converts J2000 RA/Dec to Mount Encoder Counts.

        Args:
            ra_deg (float): Right Ascension in degrees (J2000).
            dec_deg (float): Declination in degrees (J2000).
            time_offset_sec (float): Optional offset in seconds for predictive slewing.

        Returns:
            tuple[int, int]: A tuple containing (encoder_ra, encoder_dec).
        """

        # 1. Setup Time
        now = Time.now()
        if time_offset_sec != 0:
            now += time_offset_sec * u.s

        # 2. Define Target in J2000 (FK5)
        target_j2000 = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame='fk5')

        # 3. Precess to "JNow" (Apparent Place)
        target_now = target_j2000.transform_to(FK5(equinox=now))

        # 4. Calculate Hour Angle (HA)
        lmst = now.sidereal_time('mean', longitude=self.location.lon)
        ha = (lmst - target_now.ra).wrap_at(180 * u.deg)

        # 5. Extract degrees
        ha_deg = ha.deg
        dec_current_deg = target_now.dec.deg

        # 6. Apply Model & Convert
        return self._apply_linear_model(ha_deg, dec_current_deg)

    def enc_to_radec(self, ra_enc: int, dec_enc: int) -> tuple[float, float]:
        """Converts raw Encoder Counts to J2000 RA/Dec.

        Args:
            ra_enc (int): Raw encoder count for the RA axis.
            dec_enc (int): Raw encoder count for the Dec axis.

        Returns:
            tuple[float, float]: A tuple containing (RA_deg, Dec_deg) in J2000.
        """
        zeropt_ra = self.config.encoder['zeropt_ra']
        zeropt_dec = self.config.encoder['zeropt_dec']

        # 1. Reverse Linear Model (Steps -> HA/Dec degrees)
        ha_steps = ra_enc - zeropt_ra
        dec_steps = dec_enc - zeropt_dec

        ha_deg = ha_steps / self.steps_per_deg_ra
        dec_deg = dec_steps / self.steps_per_deg_dec

        # 2. Southern Hemisphere Flip
        if self.config.latitude < 0.0:
            ha_deg *= -1.0
            dec_deg *= -1.0

        # 3. Calculate Apparent RA (RA = LMST - HA)
        now = Time.now()
        lmst = now.sidereal_time('mean', longitude=self.location.lon)
        ra_apparent = lmst - (ha_deg * u.deg)

        # 4. Create Apparent SkyCoord -> Transform to J2000
        coords_now = SkyCoord(
            ra=ra_apparent,
            dec=dec_deg * u.deg,
            frame=FK5(equinox=now)
        )

        coords_j2000 = coords_now.transform_to(FK5(equinox='J2000'))

        return coords_j2000.ra.deg, coords_j2000.dec.deg

    def _apply_linear_model(self, ha_deg: float, dec_deg: float) -> tuple[int, int]:
        """Internal math to map HA/Dec degrees to encoder steps.

        Args:
            ha_deg (float): Hour Angle in degrees.
            dec_deg (float): Declination in degrees.

        Returns:
            tuple[int, int]: Calculated encoder steps (RA, Dec).
        """
        # --- CRITICAL FIX: Read Zero Points Dynamically ---
        zeropt_ra = self.config.encoder['zeropt_ra']
        zeropt_dec = self.config.encoder['zeropt_dec']

        # Logic from C: Southern Hemisphere handling
        if self.config.latitude < 0.0:
            if dec_deg > 0.0:
                dec_deg = dec_deg - 360.0
            ha_deg *= -1.0
            dec_deg *= -1.0

        # Logic from C: Wrap HA
        if ha_deg < -180.0: ha_deg += 360.0
        if ha_deg > 180.0: ha_deg -= 360.0

        # Final Scale using LIVE zero points
        enc_ra = int(ha_deg * self.steps_per_deg_ra + zeropt_ra)
        enc_dec = int(dec_deg * self.steps_per_deg_dec + zeropt_dec)

        return enc_ra, enc_dec
