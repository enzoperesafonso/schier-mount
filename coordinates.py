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

    def radec_to_enc(self, ra_deg: float, dec_deg: float) -> tuple[int, int]:
        now = Time.now()
        target_j2000 = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame=self.j2000_frame)

        # 1. Precess to JNow (Matches slaPreces in C)
        target_now = target_j2000.transform_to(FK5(equinox=now))

        # 2. Use APPARENT Sidereal Time (Matches slaGmst + slaEqeqx)
        lst = now.sidereal_time('apparent', longitude=self.location.lon)
        ha = (lst - target_now.ra).wrap_at(180 * u.deg)

        return self._apply_hardware_model(ha.deg, target_now.dec.deg)

    def _apply_hardware_model(self, ha_deg: float, dec_deg: float) -> tuple[int, int]:
        """
        Refined linear model following the ROTSE/Schier logic.
        """
        # Steps per degree from config
        s_ra = self.config.encoder['steps_per_deg_ra']
        s_dec = self.config.encoder['steps_per_deg_dec']

        # Zero points (physical limit positions)
        z_ra = self.config.encoder['zeropt_ra']
        z_dec = self.config.encoder['zeropt_dec']

        # Hemisphere Logic: Only flip the celestial direction, not the hardware offset
        if self.is_southern:
            # Shift Dec to Southern convention before flip
            if dec_deg > 0.0:
                dec_deg -= 360.0
            ha_deg *= -1.0
            dec_deg *= -1.0

        # Calculate steps relative to 0, THEN add the physical limit offset
        enc_ra = int(ha_deg * s_ra + z_ra)
        enc_dec = int(dec_deg * s_dec + z_dec)

        return enc_ra, enc_dec

    def enc_to_radec(self, ra_enc: int, dec_enc: int) -> tuple[float, float]:
        now = Time.now()

        # 1. Inverse Hardware Model
        # Subtract the limit switch offset first to get 'degrees from limit'
        ha_deg = (ra_enc - self.config.encoder['zeropt_ra']) / self.config.encoder['steps_per_deg_ra']
        dec_deg = (dec_enc - self.config.encoder['zeropt_dec']) / self.config.encoder['steps_per_deg_dec']

        if self.is_southern:
            ha_deg *= -1.0
            dec_deg *= -1.0

        # 2. Convert HA to RA using Apparent LST
        lst = now.sidereal_time('apparent', longitude=self.location.lon)
        ra_apparent = (lst.deg - ha_deg) % 360.0

        # 3. Transform back to J2000
        coords_now = SkyCoord(ra=ra_apparent * u.deg, dec=dec_deg * u.deg, frame=FK5(equinox=now))
        coords_j2000 = coords_now.transform_to(self.j2000_frame)

        return coords_j2000.ra.deg, coords_j2000.dec.deg