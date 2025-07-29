import asyncio

# when telescope is between hours angles -6 and 6 we track the RA through east (+) to west (-) using the north (+)
# side of the fork,
#
# when outside -6 to 6 hours we track the RA west (-) through east (+) using the south (-) side of the fork


class MountCoordinateTransformer:
    """Handle telescope pointing and coordinate conversion for fork-mounted equatorial mount"""

    def __init__(self, calibration_data):
        self._calibration_data = calibration_data

        self.under_pole_pointing = False

    async def astro_ha_dec_to_encoder_steps(self, ha_hours: float, dec_degrees: float,
                                            wait_for_completion: bool = True) -> bool:
        """

        """
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        # Check if coordinates are below the pole
        if abs(ha_hours) > 6:
            self.under_pole_pointing = True
            mech_ha_hours, mech_dec_degrees = self._get_under_pole_pointings(ha_hours, dec_degrees)
        else:
            self.under_pole_pointing = False
            mech_ha_hours = ha_hours
            mech_dec_degrees = self._astro_dec_degrees_to_mech_dec_degrees(dec_degrees)

        # Convert to encoder positions using the MECHANICAL coordinates
        ra_enc = self._mech_hours_to_encoder(mech_ha_hours)
        dec_enc = self._mech_dec_degrees_to_encoder(mech_dec_degrees)


        # Check bounds
        if not self._is_within_bounds(ra_enc, dec_enc):
            raise ValueError(f"Position HA={ha_hours}h, Dec={dec_degrees}° is outside mechanical limits")


        return ra_enc, dec_enc


    def _is_within_bounds(self, ha_enc: int, dec_enc: int) -> bool:
        """Check if encoder positions are within calibrated limits."""
        limits = self._calibration_data['limits']
        return (limits['ra_negative'] <= ha_enc <= limits['ra_positive'] and
                limits['dec_negative'] <= dec_enc <= limits['dec_positive'])

    def _mech_hours_to_encoder(self, ha_hours: float) -> int:
        """
        Convert Hour Angle in hours to RA encoder position.
        HA range: -6h to +6h maps to full encoder range.
        """
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        # Clamp to ±6 hours
        ha_hours = max(-6.0, min(6.0, ha_hours))

        ra_min = self._calibration_data['limits']['ra_negative']
        ra_range = self._calibration_data['ranges']['ra_encoder_range']

        # Map -6h to 0, +6h to 1
        normalized = (ha_hours + 6.0) / 12.0

        return int(ra_min + normalized * ra_range)

    def _encoder_to_mech_hours(self, ra_encoder: int) -> float:
        """
        Convert RA encoder position to Hour Angle in hours.
        """
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        ra_min = self._calibration_data['limits']['ra_negative']
        ra_range = self._calibration_data['ranges']['ra_encoder_range']

        # Clamp to encoder range
        ra_encoder = max(ra_min, min(ra_min + ra_range, ra_encoder))

        # Convert to normalized position (0 to 1)
        normalized = (ra_encoder - ra_min) / ra_range

        # Map 0 to -6h, 1 to +6h
        ha_hours = (normalized * 12.0) - 6.0

        return ha_hours

    def _mech_dec_degrees_to_encoder(self, mech_dec_degrees: float) -> int:
        """Convert mechanical declination degrees to encoder position."""
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        encoder_dec_min = self._calibration_data['limits']['dec_negative']
        encoder_dec_range = self._calibration_data['ranges']['dec_encoder_range']

        dec_range = self._calibration_data['dec_limits']['dec_angular_range']
        dec_degrees_min = self._calibration_data['dec_limits']['negative_degrees']

        encoder_value = encoder_dec_min + ((mech_dec_degrees - dec_degrees_min) / dec_range) * encoder_dec_range
        return int(encoder_value)

    def _encoder_to_mech_dec_degrees(self, mech_encoder_val: int) -> float:
        """Convert encoder position to mechanical declination degrees."""
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        encoder_dec_min = self._calibration_data['limits']['dec_negative']
        encoder_dec_range = self._calibration_data['ranges']['dec_encoder_range']

        dec_range = self._calibration_data['dec_limits']['dec_angular_range']
        dec_degrees_min = self._calibration_data['dec_limits']['negative_degrees']

        dec_degrees = dec_degrees_min + ((mech_encoder_val - encoder_dec_min) / encoder_dec_range) * dec_range
        return dec_degrees

    def _astro_dec_degrees_to_mech_dec_degrees(self, astro_dec_degrees: float) -> float:
        """
        Convert astronomical declination to mechanical declination.
        The mount maps from +122 to -113 where 122 is the + pointing direction.
        To go from astro to mechanical: add 90°
        """
        return astro_dec_degrees + 90

    def _mech_dec_degrees_to_astro_dec_degrees(self, mech_dec_degrees: float) -> float:
        """
        Convert mechanical declination to astronomical declination.
        The mount maps from +122 to -113 where 122 is the + pointing direction.
        To go from mechanical to astro: subtract 90°
        """
        return mech_dec_degrees - 90

    def _get_under_pole_pointings(self, ha_hours: float, dec_degrees: float) -> tuple[float, float]:
        """
        Gets the alternate hour angle and dec to point for targets in forbidden zone.
        These ARE mechanical coordinates for the mount.

        For under-pole pointing:
        - Mechanical HA is shifted by 12 hours (with wraparound)
        - Mechanical Dec is inverted through the pole (180° - astro_dec)
        """
        # Convert astronomical dec to mechanical coordinate system first
        # For under-pole, we need to point to the "other side" of the pole
        # This means: mech_dec = 180° - astro_dec (in mechanical coordinate system)
        mech_alt_dec_degrees = -90 - dec_degrees  # This gives us the inverted pointing

        # Alternate HA is 12 hours opposite (but clamped to ±6 range)
        if ha_hours > 0:
            mech_alt_ha_hours = ha_hours - 12.0
        else:
            mech_alt_ha_hours = ha_hours + 12.0

        # Ensure we're still within the ±6 hour mechanical range
        if mech_alt_ha_hours > 6.0:
            mech_alt_ha_hours -= 12.0
        elif mech_alt_ha_hours < -6.0:
            mech_alt_ha_hours += 12.0

        return mech_alt_ha_hours, mech_alt_dec_degrees

    def _reverse_under_pole_pointings(self, mech_ha_hours: float, mech_dec_degrees: float) -> tuple[float, float]:
        """
        Reverse the under-pole transformation to get back astronomical coordinates.
        """
        pass
