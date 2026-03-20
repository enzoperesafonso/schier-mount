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

    def radec_to_enc(self, ra_deg: float, dec_deg: float, time_offset = 0.0) -> tuple[int, int]:


        enc_ra = int(ra_deg * self.config.encoder['steps_per_deg_ra']  + self.config.encoder['zeropt_ra'])
        enc_dec = int(dec_deg * self.config.encoder['steps_per_deg_dec']  + self.config.encoder['zeropt_dec'])

        return enc_ra, enc_dec

    def enc_to_radec(self, ra_enc: int, dec_enc: int) -> tuple[float, float]:

        ha_deg = (ra_enc - self.config.encoder['zeropt_ra']) /self.config.encoder['steps_per_deg_ra']
        dec_deg = (dec_enc - self.config.encoder['zeropt_dec']) / self.config.encoder['steps_per_deg_dec']


        return ha_deg, dec_deg
