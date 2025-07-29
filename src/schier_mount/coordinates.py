from state import MountStatus

# when telescope is between hours angles -6 and 6 we track the RA through east (+) to west (-) using the north (+)
# side of the fork. When outside -6 to 6 hours we track the RA west (-) through east (+) using the south (-) side of the fork

class Coordinates:
    """Handle telescope pointing and coordinate conversion for the fork-mounted equatorial Schier mount"""
    def __init__(self, status: MountStatus, calibration_data):

        self._calibration_data = calibration_data

        self._status = status

        self._observer_latitude = calibration_data['observer_latitude']

        self._limits = self._calibration_data['limits']

        self._ha_neg_lim = self._limits['ha_negative']
        self._dec_neg_lim = self._limits['dec_negative']

        self._ha_pos_lim = self._limits['ha_positive']
        self._dec_pos_lim = self._limits['dec_positive']

        self._ha_range = self._calibration_data['ranges']['ha_encoder_range']
        self._dec_range = self._calibration_data['ranges']['dec_encoder_range']

        self._ha_steps_per_degree = self._calibration_data['ha_steps_per_degree']
        self._dec_steps_per_degree = self._calibration_data['dec_steps_per_degree']

        self._nadir_virtual_angle = - (90 + abs(self._observer_latitude))

    def ha_dec_to_encoder_positions(self, ha: float, dec: float) -> tuple[int, int, bool]:
        """
               Convert hour angle and declination to encoder positions.

               Args:
                   ha: Hour angle in hours (-12 to +12)
                   dec: Declination in degrees (-90 to +90)

               Returns:
                   Tuple of (ha_encoder, dec_encoder, below_pole)
               """

        below_pole = False

        if abs(ha) > 6:
            below_pole = True

            virtual_dec = - (dec + 90)

            # flip around HA axis pointing
            if ha > 0:
                virtual_ha = ha - 12.0
            else:
                virtual_ha = ha + 12.0

        else:
            virtual_dec = (dec + 90)
            virtual_ha = ha

        print(f'virtual_dec = {virtual_dec}, virtual_ha = {virtual_ha}')

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
            Tuple of (ha, dec) where:
            - ha: Hour angle in hours (-12 to +12)
            - dec: Declination in degrees (-90 to +90)
            - below_pole: True if the encoder position is below positive degrees
        """
        # Convert encoder positions to virtual coordinates
        virtual_ha = self._encoder_ha_to_virtual(ha_enc)
        virtual_dec = self._encoder_dec_to_virtual(dec_enc)

        # Determine if we're in below_pole mode based on virtual_dec sign
        # When virtual_dec is negative, we were in below_pole mode
        below_pole = virtual_dec < 0

        if below_pole:
            # Reverse the below_pole transformations from ha_dec_to_encoder_positions
            # virtual_dec = -(dec + 90), so dec = -virtual_dec - 90
            dec = -virtual_dec - 90

            # Reverse the HA flip: if virtual_ha was ha - 12, then ha = virtual_ha + 12
            # if virtual_ha was ha + 12, then ha = virtual_ha - 12
            # We can determine which case based on virtual_ha sign
            if virtual_ha >= 0:
                # This was the case where original ha > 0, so virtual_ha = ha - 12
                ha = virtual_ha - 12
            else:
                # This was the case where original ha < 0, so virtual_ha = ha + 12
                ha = virtual_ha + 12

        else:
            # Normal mode: virtual_dec = dec + 90, so dec = virtual_dec - 90
            dec = virtual_dec - 90
            # virtual_ha = ha, so ha = virtual_ha
            ha = virtual_ha

        return ha, dec, below_pole

    def _virtual_dec_to_encoder(self, v_dec: float) -> int:
        encoder_position =  self._dec_neg_lim + ((self._nadir_virtual_angle + v_dec) * self._dec_steps_per_degree)
        return int(round(encoder_position))

    def _encoder_dec_to_virtual(self, enc_dec: int) -> float:
        encoder_offset = enc_dec - self._dec_neg_lim
        offset_from_scp = (encoder_offset / self._dec_steps_per_degree) - self._nadir_virtual_angle
        return offset_from_scp

    def _virtual_ha_to_encoder(self, v_ha: float) -> int:
        """
        Convert virtual Hour Angle (-6h to +6h) to encoder steps.

        Parameters:
        - ha_hours: Desired hour angle in hours (-6 to +6)
        - ha_min_encoder: Encoder value at -6h
        - ha_max_encoder: Encoder value at +6h

        Returns:
        - encoder_value: Corresponding encoder position
        """
        assert -6.0 <= v_ha <= 6.0, "HA out of range"

        # Normalize from [-6h, +6h] → [0.0, 1.0]
        frac = (v_ha + 6.0) / 12.0

        encoder_value = self._ha_neg_lim + frac * self._ha_range

        return int(round(encoder_value))



    def _encoder_ha_to_virtual(self, enc_ha):
        """
        Convert encoder value to virtual HA (-6h to +6h).

        Parameters:
        - encoder_value: Measured encoder value

        Returns:
        - ha_hours: Hour angle in hours
        """

        frac = (enc_ha - self._ha_neg_lim) /  self._ha_range
        ha_hours = frac * 12.0 - 6.0

        return ha_hours
