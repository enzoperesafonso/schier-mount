import asyncio
from typing import Tuple, Optional
from datetime import datetime
import math


class SchierMountPointing:
    """Handle telescope pointing and coordinate conversion for fork-mounted equatorial mount"""

    def __init__(self, Comm, calibration_data, observatory_latitude):
        self._Comm = Comm
        self._calibration_data = calibration_data
        self._observatory_latitude = observatory_latitude
        self.ha_tracking_side = 'west'
        self.dec_tracking_side = 'north'

    async def goto_ha_dec(self, ha_hours: float, dec_degrees: float,
                          wait_for_completion: bool = True) -> bool:
        """
        Move telescope to specified Hour Angle and Declination coordinates.

        Args:
            ha_hours: Hour Angle in hours (-6 to +6)
            dec_degrees: Declination in degrees
            wait_for_completion: Whether to wait for motion to complete

        Returns:
            True if successful, False otherwise
        """
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        # Check if coordinates are within limits
        if abs(ha_hours) > 6.0:
            raise ValueError(f"Hour Angle {ha_hours}h is outside ±6h limits")

        # Convert to encoder positions
        ra_enc = self._ha_hours_to_encoder(ha_hours)
        dec_enc = self._dec_degrees_to_encoder(dec_degrees)

        # Check bounds
        if not await self._is_within_bounds(ra_enc, dec_enc):
            raise ValueError(f"Position HA={ha_hours}h, Dec={dec_degrees}° is outside mechanical limits")

        # Determine tracking sides for this position
        self.ha_tracking_side, self.dec_tracking_side = self._determine_track_sides(ha_hours, dec_degrees)

        # Move to position
        await self._Comm.move_enc(ra_enc, dec_enc)

        if wait_for_completion:
            # Wait for motion to complete (you might want to implement this based on your system)
            await asyncio.sleep(2)  # Simple wait - replace with proper motion detection

        return True

    async def get_ha_dec(self) -> Tuple[float, float]:
        """
        Get current Hour Angle and Declination.

        Returns:
            Tuple of (ha_hours, dec_degrees)
        """
        ra_enc, dec_enc = await self._Comm.get_encoder_positions()

        ha_hours = self._encoder_to_ha_hours(ra_enc)
        dec_degrees = self._encoder_to_dec_degrees(dec_enc)

        return ha_hours, dec_degrees

    async def stop_motion(self):
        """Stop all telescope motion."""
        await self._Comm.stop()

    async def _is_within_bounds(self, ha_enc: int, dec_enc: int) -> bool:
        """Check if encoder positions are within calibrated limits."""
        limits = self._calibration_data['limits']
        return (limits['ra_negative'] <= ha_enc <= limits['ra_positive'] and
                limits['dec_negative'] <= dec_enc <= limits['dec_positive'])

    def _ha_hours_to_encoder(self, ha_hours: float) -> int:
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

    def _encoder_to_ha_hours(self, ra_encoder: int) -> float:
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

    def _dec_degrees_to_encoder(self, dec_degrees: float) -> int:
        """
        Convert declination in degrees to encoder position.
        Based on your specifications:
        - Dec positive limit: 122° from SCP (northward)
        - Dec negative limit: 113° from SCP (southward)
        - Total angular range: 235°
        """
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        # Calculate actual declination limits from SCP distances
        # SCP is at -90° declination
        dec_positive_limit = -90.0 + 122.0  # +32° declination (northward)
        dec_negative_limit = -90.0 - 113.0  # -203° declination (southward, but clamp to -90°)
        dec_negative_limit = max(dec_negative_limit, -90.0)  # Can't go past south pole

        # Clamp input declination to valid range
        dec_degrees = max(dec_negative_limit, min(dec_positive_limit, dec_degrees))

        dec_min = self._calibration_data['limits']['dec_negative']
        dec_max = self._calibration_data['limits']['dec_positive']
        dec_range = dec_max - dec_min

        # Map declination range to encoder range
        dec_span = dec_positive_limit - dec_negative_limit
        relative_dec = dec_degrees - dec_negative_limit

        encoder_value = dec_min + (relative_dec / dec_span) * dec_range
        return int(encoder_value)

    def _encoder_to_dec_degrees(self, dec_encoder: int) -> float:
        """
        Convert encoder value to declination in degrees.
        Based on angular distances from South Celestial Pole:
        - Positive limit: 122° from SCP = +32° declination
        - Negative limit: 113° from SCP = -90° declination (clamped at south pole)
        """
        if not self._calibration_data['calibrated']:
            raise ValueError("Telescope not calibrated!")

        # Calculate actual declination limits from SCP distances
        dec_positive_limit = -90.0 + 122.0  # +32° declination
        dec_negative_limit = -90.0 - 113.0  # -203° but clamped to -90°
        dec_negative_limit = max(dec_negative_limit, -90.0)

        dec_min = self._calibration_data['limits']['dec_negative']
        dec_max = self._calibration_data['limits']['dec_positive']
        dec_range = dec_max - dec_min

        # Clamp to encoder range
        dec_encoder = max(dec_min, min(dec_max, dec_encoder))

        # Convert encoder position to declination
        encoder_offset = dec_encoder - dec_min
        dec_span = dec_positive_limit - dec_negative_limit

        declination = dec_negative_limit + (encoder_offset / dec_range) * dec_span

        return declination

    def _determine_track_sides(self, ha_hours: float, dec_degrees: float) -> Tuple[str, str]:
        """
        Determine tracking sides based on position.

        Returns:
            Tuple of (ha_tracking_side, dec_tracking_side)
        """
        # For fork mount, tracking side depends on which side of meridian
        ha_tracking_side = 'east' if ha_hours < 0 else 'west'

        # Dec tracking side based on hemisphere
        dec_tracking_side = 'south' if dec_degrees < 0 else 'north'

        return ha_tracking_side, dec_tracking_side

    def _needs_repointing_due_to_ha_limit(self, ha_hours: float) -> bool:
        """
        Determines if the telescope needs to be repointed because it's
        approaching the HA tracking limit at ±6 hours.
        """
        max_ha_hours = 6.0
        safety_margin = 0.1  # in hours, ~6 minutes

        return abs(ha_hours) >= (max_ha_hours - safety_margin)

    def get_pointing_info(self) -> dict:
        """
        Get current pointing information including limits and current position.
        """
        if not self._calibration_data['calibrated']:
            return {"calibrated": False}

        # Calculate declination limits from SCP distances
        dec_positive_limit = -90.0 + 122.0  # +32°
        dec_negative_limit = max(-90.0 - 113.0, -90.0)  # -90° (clamped at south pole)

        return {
            "calibrated": True,
            "ha_limits": {
                "negative_hours": -6.0,
                "positive_hours": 6.0
            },
            "dec_limits": {
                "negative_degrees": dec_negative_limit,
                "positive_degrees": dec_positive_limit,
                "total_angular_range": 235.0  # 122° + 113°
            },
            "current_tracking_sides": {
                "ha_side": self.ha_tracking_side,
                "dec_side": self.dec_tracking_side
            },
            "observatory_latitude": self._observatory_latitude
        }