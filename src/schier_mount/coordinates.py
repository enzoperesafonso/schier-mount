from state import MountStatus

# Fork-mounted equatorial telescope coordinate transformations
# When |HA| <= 6h: Normal tracking using north (+) side of fork (RA increases east to west)
# When |HA| > 6h: Below-pole tracking using south (-) side of fork (through the pole)

class Coordinates:
    """Handle telescope pointing and coordinate conversion for the fork-mounted equatorial Schier mount"""

    def __init__(self, status: MountStatus, calibration_data):
        self._calibration_data = calibration_data
        self._status = status
        self._observer_latitude = calibration_data['observer_latitude'] + 4

        # Mount limits and ranges
        self._limits = calibration_data['limits']
        self._ha_neg_lim = self._limits['ha_negative']
        self._dec_neg_lim = self._limits['dec_negative']
        self._ha_pos_lim = self._limits['ha_positive']
        self._dec_pos_lim = self._limits['dec_positive']

        self._ha_range = self._calibration_data['ranges']['ha_encoder_range']
        self._dec_range = self._calibration_data['ranges']['dec_encoder_range']

        self._dec_steps_per_degree = calibration_data['dec_steps_per_degree']

        # Calculate the virtual angle for nadir (straight down) position
        # This is the declination angle when pointing at nadir from observer's position
        self._nadir_virtual_angle = -(90 + abs(self._observer_latitude))

    def ha_dec_to_encoder_positions(self, ha: float, dec: float) -> tuple[int, int, bool]:
        """
        Convert hour angle and declination to encoder positions.

        For fork mounts:
        - Normal mode (|HA| <= 6h): Direct mapping, telescope tracks normally
        - Below-pole mode (|HA| > 6h): Telescope flips through pole to avoid collision

        Args:
            ha: Hour angle in hours (-12 to +12)
            dec: Declination in degrees (-90 to +90)

        Returns:
            Tuple of (ha_encoder, dec_encoder, below_pole)
        """
        # Normalize HA to -12 to +12 range
        ha = self._normalize_ha(ha)

        # Determine if we need below-pole pointing
        below_pole = abs(ha) > 6.0

        if below_pole:
            # Below-pole mode: telescope points through the pole
            # The declination becomes negative of (dec + 90) to flip through pole
            virtual_dec = -(dec + 90)

            # Adjust HA by 12 hours and constrain to ±6h range
            if ha > 0:
                virtual_ha = ha - 12.0  # Positive HA becomes negative
            else:
                virtual_ha = ha + 12.0  # Negative HA becomes positive

        else:
            # Normal mode: direct mapping
            virtual_dec = dec + 90  # Offset declination to make it positive
            virtual_ha = ha

        print(
            f'HA={ha:.3f}h, Dec={dec:.1f}° -> virtual_ha={virtual_ha:.3f}h, virtual_dec={virtual_dec:.1f}°, below_pole={below_pole}')

        # Convert virtual coordinates to encoder positions
        ha_encoder = self._virtual_ha_to_encoder(virtual_ha)
        dec_encoder = self._virtual_dec_to_encoder(virtual_dec)

        return ha_encoder, dec_encoder, below_pole

    def encoder_positions_to_ha_dec(self, ha_enc: int, dec_enc: int) -> tuple[float, float, bool]:
        """
        Convert encoder positions to hour angle and declination.

        Args:
            ha_enc: Hour angle encoder position
            dec_enc: Declination encoder position

        Returns:
            Tuple of (ha, dec, below_pole) where:
            - ha: Hour angle in hours (-12 to +12)
            - dec: Declination in degrees (-90 to +90)
            - below_pole: True if in below-pole configuration
        """
        # Convert encoder positions to virtual coordinates
        virtual_ha = self._encoder_ha_to_virtual(ha_enc)
        virtual_dec = self._encoder_dec_to_virtual(dec_enc)

        # Determine mode based on virtual_dec sign
        below_pole = virtual_dec < -1

        if below_pole:
            # Reverse below-pole transformations
            # virtual_dec = -(dec + 90) -> dec = -virtual_dec - 90
            dec = -virtual_dec - 90

            # Reverse HA transformation
            # We need to add/subtract 12h and ensure result is in valid range
            if virtual_ha > 0:
                # This came from ha > 0, virtual_ha = ha - 12
                ha = virtual_ha + 12.0
            else:
                # This came from ha < 0, virtual_ha = ha + 12
                ha = virtual_ha - 12.0

        else:
            # Normal mode: reverse direct mapping
            dec = virtual_dec - 90  # Remove the +90 offset
            ha = virtual_ha  # Direct mapping

        # Normalize HA to proper range
        ha = self._normalize_ha(ha)

        return ha, dec, below_pole

    def _virtual_dec_to_encoder(self, v_dec: float) -> int:
        """
        Convert virtual declination to encoder position.

        The virtual declination coordinate system:
        - 0° = nadir (straight down)
        - 90° = celestial pole
        - 180° = zenith (straight up)
        """
        # Calculate offset from nadir position
        offset_from_nadir = v_dec - self._nadir_virtual_angle

        # Convert to encoder steps
        encoder_position = self._dec_neg_lim + (offset_from_nadir * self._dec_steps_per_degree)

        return int(round(encoder_position))

    def _encoder_dec_to_virtual(self, enc_dec: int) -> float:
        """Convert encoder position to virtual declination."""
        # Calculate offset from negative limit
        encoder_offset = enc_dec - self._dec_neg_lim

        # Convert to degrees and add nadir offset
        virtual_dec = (encoder_offset / self._dec_steps_per_degree) + self._nadir_virtual_angle

        return virtual_dec

    def _virtual_ha_to_encoder(self, v_ha: float) -> int:
        """
        Convert virtual Hour Angle (-6h to +6h) to encoder steps.

        Note: RA axis is inverted - positive encoder limit = -6h, negative limit = +6h

        Args:
            v_ha: Virtual hour angle in hours (-6 to +6)

        Returns:
            encoder_value: Corresponding encoder position
        """
        # Validate input range
        if not (-6.0 <= v_ha <= 6.0):
            raise ValueError(f"Virtual HA {v_ha:.3f}h out of range [-6h, +6h]")

        # Invert the mapping: -6h maps to positive limit, +6h maps to negative limit
        # Normalize from [-6h, +6h] to [1.0, 0.0] (inverted)
        fraction = (-v_ha + 6.0) / 12.0

        # Map to encoder range
        encoder_value = self._ha_neg_lim + fraction * self._ha_range

        return int(round(encoder_value))

    def _encoder_ha_to_virtual(self, enc_ha: int) -> float:
        """
        Convert encoder value to virtual HA (-6h to +6h).

        Note: RA axis is inverted - positive encoder limit = -6h, negative limit = +6h

        Args:
            enc_ha: Encoder value

        Returns:
            ha_hours: Virtual hour angle in hours
        """
        # Calculate fraction across encoder range
        fraction = (enc_ha - self._ha_neg_lim) / self._ha_range

        # Map from [0.0, 1.0] to [+6h, -6h] (inverted mapping)
        ha_hours = 6.0 - fraction * 12.0

        return ha_hours

    def _normalize_ha(self, ha: float) -> float:
        """
        Normalize hour angle to the range [-12, +12] hours.

        Args:
            ha: Hour angle in hours

        Returns:
            Normalized hour angle in range [-12, +12]
        """
        # Handle values outside ±12h range
        while ha > 12.0:
            ha -= 24.0
        while ha < -12.0:
            ha += 24.0

        return ha

    def get_flip_position(self, ha: float, dec: float) -> tuple[float, float]:
        """
        Calculate the equivalent position on the opposite side of the pole.

        This is useful for determining alternative pointing positions
        and meridian flip targets.

        Args:
            ha: Current hour angle in hours
            dec: Current declination in degrees

        Returns:
            Tuple of (flip_ha, flip_dec) for the equivalent position
        """
        # The flip position is 12h away in HA with complementary declination
        if ha >= 0:
            flip_ha = ha - 12.0
        else:
            flip_ha = ha + 12.0

        # For declination, the flip position has the complementary angle
        # through the pole: flip_dec = 180° - dec
        flip_dec = 180.0 - dec

        # Normalize the results
        flip_ha = self._normalize_ha(flip_ha)

        # Keep declination in valid range
        if flip_dec > 90.0:
            flip_dec = 180.0 - flip_dec
            flip_ha = self._normalize_ha(flip_ha + 12.0)
        elif flip_dec < -90.0:
            flip_dec = -180.0 - flip_dec
            flip_ha = self._normalize_ha(flip_ha + 12.0)

        return flip_ha, flip_dec

    def needs_meridian_flip(self, ha: float, dec: float, horizon_limit: float = 6.0) -> bool:
        """
        Determine if a meridian flip is needed to avoid hitting limits.

        Args:
            ha: Hour angle in hours
            dec: Declination in degrees
            horizon_limit: Hour angle limit before flip is needed (default 6h)

        Returns:
            True if meridian flip is recommended
        """
        return abs(ha) > horizon_limit

    def is_position_reachable(self, ha: float, dec: float) -> bool:
        """
        Check if a given HA/Dec position is mechanically reachable.

        Args:
            ha: Hour angle in hours
            dec: Declination in degrees

        Returns:
            True if position is reachable by the mount
        """
        # Normalize inputs
        ha = self._normalize_ha(ha)

        # Check declination limits (basic check)
        if not (-90.0 <= dec <= 90.0):
            return False

        try:
            # Try to convert to encoder positions
            ha_enc, dec_enc, below_pole = self.ha_dec_to_encoder_positions(ha, dec)

            # Check if encoder positions are within physical limits
            ha_in_range = self._ha_neg_lim <= ha_enc <= self._ha_pos_lim
            dec_in_range = self._dec_neg_lim <= dec_enc <= self._dec_pos_lim

            return ha_in_range and dec_in_range

        except (ValueError, ZeroDivisionError):
            return False