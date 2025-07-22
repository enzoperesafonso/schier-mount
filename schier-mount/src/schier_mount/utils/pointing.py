import asyncio
from typing import Tuple, Optional
from datetime import datetime

class SchierMountPointing:
    """Handle telescope pointing and coordinate conversion"""

    def __init__(self, Comm, calibration_data, observatory_latitude):
        self._Comm = Comm
        self._calibration_data = calibration_data
        self._observatory_latitude = observatory_latitude
        self.ha_tracking_side = 'west'
        self.dec_tracking_side = 'north'

    async def goto_ha_dec(self, ha_hours: float, dec_degrees: float,
                          wait_for_completion: bool = True) -> bool:
        pass

    async def get_ha_dec(self):

        pass


    async def stop_motion(self):
        await self._Comm.stop()

    async def _is_within_bounds(self, ha_enc: int, dec_enc: int) -> bool:
        limits = self._calibration_data['limits']
        return (limits['ra_negative'] <= ha_enc <= limits['ra_positive'] and
                limits['dec_negative'] <= dec_enc <= limits['dec_positive'])

    # mechanical degrees refers to the limit switch mappings between -90* to 90* and -6hrs to 6hrs

    def _ra_encoder_to_mech_degrees(self, encoder_ra: int) -> float:
        ra_min = self._calibration_data['limits']['ra_negative']
        ra_range = self._calibration_data['ranges']['ra_encoder_range']
        return 180.0 * (encoder_ra - ra_min) / ra_range - 90.0  # Maps -90 to +90

    def _dec_encoder_to_mech_degrees(self, encoder_dec: int) -> float:
        """Convert encoder value to declination in degrees (mechanical angle)."""
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        nadir_dec = - (90.0 - self._observatory_latitude)
        zenith_dec = self._observatory_latitude

        dec_min = self._calibration_data['limits']['dec_negative']
        dec_max = self._calibration_data['limits']['dec_positive']
        dec_range = dec_max - dec_min

        # Clamp to encoder range just in case
        encoder_dec = max(dec_min, min(dec_max, encoder_dec))

        # Convert encoder position to declination
        encoder_offset = encoder_dec - dec_min
        declination = nadir_dec + (encoder_offset / dec_range) * (zenith_dec - nadir_dec)

        return declination

    def _ra_mech_degrees_to_encoder(self, ra_deg: float) -> int:
        ra_min = self._calibration_data['limits']['ra_negative']
        ra_range = self._calibration_data['ranges']['ra_encoder_range']
        return int(ra_min + ((ra_deg + 90.0) / 180.0) * ra_range)

    def _dec_mech_degrees_to_encoder(self, dec_deg: float) -> int:
        """Convert declination in degrees to encoder position."""
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        nadir_dec = - (90.0 - self._observatory_latitude)
        zenith_dec = self._observatory_latitude

        # Clamp input declination to valid range
        dec_deg = max(nadir_dec, min(zenith_dec, dec_deg))

        dec_min = self._calibration_data['limits']['dec_negative']
        dec_max = self._calibration_data['limits']['dec_positive']
        dec_range = dec_max - dec_min

        relative_dec = dec_deg - nadir_dec
        total_dec_span = zenith_dec - nadir_dec

        encoder_value = dec_min + (relative_dec / total_dec_span) * dec_range
        return int(encoder_value)

    def _determine_track_sides(self, ha_hours: float, dec_degrees: float) -> str:
        ha_tracking_side = 'east' if abs(ha_hours) < 6 else 'west'
        dec_tracking_side = 'north' if dec_degrees < 0 else 'south'
        return ha_tracking_side, dec_tracking_side

    def _needs_repointing_due_to_ha_limit(self, ha_hours: float) -> bool:
        """
        Determines if the telescope needs to be repointed because it's
        approaching the HA tracking limit at ±6 hours.
        """
        max_ha_hours = 6.0
        safety_margin = 0.1  # in hours, ~6 minutes

        return abs(ha_hours) >= (max_ha_hours - safety_margin)

