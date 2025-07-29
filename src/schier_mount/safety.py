class Safety:

    def __init__(self, calibration_data):
        self._calibration_data = calibration_data
        self._limits = self._calibration_data['limits']

        self._ha_neg_lim = self._limits['ha_negative']
        self._dec_neg_lim = self._limits['dec_negative']

        self._ha_pos_lim = self._limits['ha_positive']
        self._dec_pos_lim = self._limits['dec_positive']

        self._ha_range = self._calibration_data['ranges']['ha_encoder_range']
        self._dec_range = self._calibration_data['ranges']['dec_encoder_range']

        # safety factors are calculated using entire range
        self._ha_buffer_steps = self._ha_range * self._calibration_data['limits_safety_factor']
        self._dec_buffer_steps = self._dec_range * self._calibration_data['limits_safety_factor']

    def enc_position_is_within_safety_limits(self, ha_enc: int, dec_enc: int) -> bool:
        """Check if encoder positions are within calibrated limits."""

        return ((self._ha_neg_lim + self._ha_buffer_steps) <= ha_enc <= (self._ha_pos_lim - self._ha_buffer_steps)

                and

                (self._dec_neg_lim + self._dec_buffer_steps) <= dec_enc <= (self._dec_pos_lim - self._dec_buffer_steps)

                )