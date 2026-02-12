import logging
from astropy.coordinates import SkyCoord, EarthLocation, FK5
from astropy.time import Time
from astropy import units as u


class MountCoordinates:
    """
    Optimized Coordinate Conversion Module for SchierMount.
    Designed for high-frequency telemetry updates in async drivers.

    This class handles the transformation between celestial coordinates (J2000 RA/Dec)
    and the physical motor encoder positions of the mount. It accounts for:
    - Precession from J2000 to the current epoch (JNow).
    - Conversion between Right Ascension and Hour Angle based on Local Mean Sidereal Time.
    - Hemispheric orientation (Northern vs. Southern).
    - Linear scaling of degrees to encoder steps.

    Attributes:
        config: Configuration object containing location and encoder parameters.
        location (EarthLocation): Cached Astropy EarthLocation for sidereal time calculations.
        j2000_frame (FK5): Cached coordinate frame for J2000 equinox.
        steps_per_deg_ra (float): Encoder resolution for the Right Ascension axis.
        steps_per_deg_dec (float): Encoder resolution for the Declination axis.
        is_southern (bool): Flag indicating if the mount is located in the Southern Hemisphere.
    """

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger("SchierMount.Coords")

        # 1. Cache static location and frames to avoid re-instantiation
        self.location = EarthLocation(
            lon=self.config.location['longitude'] * u.deg,
            lat=self.config.location['latitude'] * u.deg,
            height=self.config.location['elevation'] * u.m
        )
        self.j2000_frame = FK5(equinox='J2000')

        # Cache static scale factors
        self.steps_per_deg_ra = self.config.encoder['steps_per_deg_ra']
        self.steps_per_deg_dec = self.config.encoder['steps_per_deg_dec']
        self.is_southern = self.config.location['latitude'] < 0.0

    def radec_to_enc(self, ra_deg: float, dec_deg: float, time_offset_sec: float = 0.0) -> tuple[int, int]:
        """
        Converts J2000 RA/Dec coordinates to motor encoder steps.

        Args:
            ra_deg (float): Right Ascension in degrees (J2000).
            dec_deg (float): Declination in degrees (J2000).
            time_offset_sec (float): Optional look-ahead offset in seconds.

        Returns:
            tuple[int, int]: A tuple containing (ra_encoder_steps, dec_encoder_steps).
        """
        now = Time.now()
        if time_offset_sec != 0:
            now += time_offset_sec * u.s

        target_j2000 = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame=self.j2000_frame)

        # Precess to Current Equinox (Apparent Place)
        target_now = target_j2000.transform_to(FK5(equinox=now))

        # Calculate Hour Angle
        lmst = now.sidereal_time('mean', longitude=self.location.lon)
        ha = (lmst - target_now.ra).wrap_at(180 * u.deg)

        return self._apply_linear_model(ha.deg, target_now.dec.deg)

    def enc_to_radec(self, ra_enc: int, dec_enc: int, timestamp: float = None) -> tuple[float, float]:
        """
        High-frequency conversion for telemetry.
        Accepts an optional Unix timestamp for exact synchronicity. This method
        performs the inverse of the linear model, converting raw encoder counts
        back into J2000 Right Ascension and Declination.

        Args:
            ra_enc (int): Raw encoder steps for the RA axis.
            dec_enc (int): Raw encoder steps for the Dec axis.
            timestamp (float, optional): Unix timestamp for the observation.

        Returns:
            tuple[float, float]: (RA, Dec) in degrees J2000.
        """
        # 1. Reverse Linear Model
        # Using local variables for speed to avoid multiple dict lookups
        zeropt_ra = self.config.encoder['zeropt_ra']
        zeropt_dec = self.config.encoder['zeropt_dec']

        ha_deg = (ra_enc - zeropt_ra) / self.steps_per_deg_ra
        dec_deg = (dec_enc - zeropt_dec) / self.steps_per_deg_dec

        # 2. Southern Hemisphere Flip & Dec Normalization
        if self.is_southern:
            ha_deg *= -1.0
            dec_deg *= -1.0

        # Ensure Dec is within physical limits [-90, 90]
        if abs(dec_deg) > 90:
            dec_deg = self._normalize_dec(dec_deg)

        # 3. Time Handling (Use provided timestamp if available)
        now = Time(timestamp, format='unix') if timestamp else Time.now()
        lmst = now.sidereal_time('mean', longitude=self.location.lon)

        # RA = LMST - HA (wrapped to 0-360)
        ra_apparent = (lmst.deg - ha_deg) % 360.0

        # 4. Transform back to J2000 for display/reporting
        # This is the most expensive line.
        coords_now = SkyCoord(ra=ra_apparent * u.deg, dec=dec_deg * u.deg, frame=FK5(equinox=now))
        coords_j2000 = coords_now.transform_to(self.j2000_frame)

        return coords_j2000.ra.deg, coords_j2000.dec.deg

    def _apply_linear_model(self, ha_deg: float, dec_deg: float) -> tuple[int, int]:
        """
        Maps Hour Angle and Declination degrees to raw encoder steps.

        Args:
            ha_deg (float): Hour Angle in degrees.
            dec_deg (float): Declination in degrees.

        Returns:
            tuple[int, int]: (RA encoder steps, Dec encoder steps).
        """
        zeropt_ra = self.config.encoder['zeropt_ra']
        zeropt_dec = self.config.encoder['zeropt_dec']

        if self.is_southern:
            # Handle the 180-degree flip logic for southern declination
            if dec_deg > 0.0:
                dec_deg -= 360.0
            ha_deg *= -1.0
            dec_deg *= -1.0

        # Wrap HA to [-180, 180]
        ha_deg = (ha_deg + 180) % 360 - 180

        enc_ra = int(ha_deg * self.steps_per_deg_ra + zeropt_ra)
        enc_dec = int(dec_deg * self.steps_per_deg_dec + zeropt_dec)

        return enc_ra, enc_dec

    @staticmethod
    def _normalize_dec(dec: float) -> float:
        """Corrects declination fold-over at the poles."""
        dec = (dec + 180) % 360 - 180
        if dec > 90:
            return 180 - dec
        if dec < -90:
            return -180 - dec
        return dec