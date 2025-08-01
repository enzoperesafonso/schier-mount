from enum import Enum
from dataclasses import dataclass
from typing import Tuple
import math


class ForkSide(Enum):
    """Which side of the fork the telescope is using"""
    NORTH = "north"  # Normal tracking, HA in [-6, +6]
    SOUTH = "south"  # Flipped tracking, HA outside [-6, +6]


@dataclass
class MountPosition:
    """Physical mount position - always uses ±6 hour HA range"""
    ha: float  # Hour angle in hours [-6, +6] (physical mount angle)
    dec: float  # Declination in degrees [-90, +90]
    side: ForkSide  # Which side of fork we're using


class ForkMountCoordinates:
    """
    Clean coordinate system for fork mount telescopes.

    Key insight: Separate the CELESTIAL coordinates (what you want to point at)
    from the MOUNT coordinates (how the mount physically positions itself).
    """

    def __init__(self, calibration_data):
        self.cal = calibration_data

        # Physical encoder limits and ranges
        self.ha_encoder_min = calibration_data['limits']['ha_negative']
        self.ha_encoder_max = calibration_data['limits']['ha_positive']
        self.dec_encoder_min = calibration_data['limits']['dec_negative']
        self.dec_encoder_max = calibration_data['limits']['dec_positive']

        self.ha_encoder_range = calibration_data['ranges']['ha_encoder_range']
        self.dec_encoder_range = calibration_data['ranges']['dec_encoder_range']

        # Steps per degree
        self.ha_steps_deg = calibration_data['ha_steps_per_degree']
        self.dec_steps_deg = calibration_data['dec_steps_per_degree']

        # Physical reference points
        self.observer_lat = calibration_data['observer_latitude']

        # Encoder positions at key points
        self.ha_encoder_at_zero = self.ha_encoder_min + self.ha_encoder_range / 2  # 0h position

        # CRITICAL: Dec encoder at negative limit points to NADIR (straight down)
        # Nadir is at altitude = -90°, which corresponds to declination = -(90° + observer_latitude)
        self.nadir_declination = -(90.0 + abs(self.observer_lat))
        self.dec_encoder_at_nadir = self.dec_encoder_min  # Points straight down

    def celestial_to_mount(self, celestial_ha: float, celestial_dec: float) -> MountPosition:
        """
        Convert celestial coordinates to mount position.

        This is where all the fork mount logic lives!
        """
        # Normalize celestial HA to [-12, +12]
        celestial_ha = self._normalize_ha_24h(celestial_ha)

        if -6.0 <= celestial_ha <= 6.0:
            # NORTH SIDE: Direct tracking
            side = ForkSide.NORTH
            mount_ha = celestial_ha
            mount_dec = celestial_dec

        else:
            # SOUTH SIDE: Flip through the meridian
            side = ForkSide.SOUTH

            # Map HA to equivalent ±6h position
            if celestial_ha > 6.0:
                mount_ha = celestial_ha - 12.0  # +8h → -4h
            else:  # celestial_ha < -6.0
                mount_ha = celestial_ha + 12.0  # -8h → +4h

            # When looking through the fork to the south side,
            # declination angles are measured from the opposite pole
            mount_dec = 180.0 - celestial_dec

        return MountPosition(mount_ha, mount_dec, side)

    def mount_to_celestial(self, mount_pos: MountPosition) -> Tuple[float, float]:
        """Convert mount position back to celestial coordinates."""

        if mount_pos.side == ForkSide.NORTH:
            # Direct mapping
            celestial_ha = mount_pos.ha
            celestial_dec = mount_pos.dec

        else:  # ForkSide.SOUTH
            # Reverse the south side transformations
            if mount_pos.ha >= 0:
                celestial_ha = mount_pos.ha + 12.0  # -4h → +8h
            else:
                celestial_ha = mount_pos.ha - 12.0  # +4h → -8h

            celestial_dec = 180.0 - mount_pos.dec

        # Normalize result
        celestial_ha = self._normalize_ha_24h(celestial_ha)

        return celestial_ha, celestial_dec

    def mount_to_encoders(self, mount_pos: MountPosition) -> Tuple[int, int]:
        """Convert mount position to encoder values."""

        # HA encoder: linear mapping over ±6h range
        ha_encoder = self._ha_to_encoder(mount_pos.ha)

        # Dec encoder: depends on which side of fork
        if mount_pos.side == ForkSide.NORTH:
            dec_encoder = self._dec_to_encoder_north_side(mount_pos.dec)
        else:
            dec_encoder = self._dec_to_encoder_south_side(mount_pos.dec)

        return ha_encoder, dec_encoder

    def encoders_to_mount(self, ha_encoder: int, dec_encoder: int) -> MountPosition:
        """Convert encoder values to mount position."""

        # HA is straightforward
        mount_ha = self._encoder_to_ha(ha_encoder)

        # For Dec, we need to figure out which side we're on
        # Try both interpretations and see which makes more physical sense
        dec_north = self._encoder_to_dec_north_side(dec_encoder)
        dec_south = self._encoder_to_dec_south_side(dec_encoder)

        # Decide based on which declination is in valid range
        # This is a simplification - you might have better logic based on your setup
        if -90 <= dec_north <= 90 and abs(dec_north) <= abs(dec_south):
            return MountPosition(mount_ha, dec_north, ForkSide.NORTH)
        else:
            return MountPosition(mount_ha, dec_south, ForkSide.SOUTH)

    # Encoder conversion methods
    def _ha_to_encoder(self, ha: float) -> int:
        """Convert HA in ±6h range to encoder position."""
        if not (-6.0 <= ha <= 6.0):
            raise ValueError(f"Mount HA {ha} outside ±6h range")

        # Linear mapping: -6h → min, +6h → max
        frac = (ha + 6.0) / 12.0
        encoder = self.ha_encoder_min + frac * self.ha_encoder_range
        return int(round(encoder))

    def _encoder_to_ha(self, encoder: int) -> float:
        """Convert encoder position to HA."""
        frac = (encoder - self.ha_encoder_min) / self.ha_encoder_range
        return frac * 12.0 - 6.0

    def _dec_to_encoder_north_side(self, dec: float) -> int:
        """
        Convert declination to encoder when using north side of fork.

        Physical reality: dec_encoder_min points to NADIR (straight down).
        Nadir declination = -(90° + observer_latitude)
        """
        # Calculate angular distance from nadir
        angle_from_nadir = dec - self.nadir_declination

        # Convert to encoder steps
        encoder = self.dec_encoder_min + angle_from_nadir * self.dec_steps_deg
        return int(round(encoder))

    def _encoder_to_dec_north_side(self, encoder: int) -> float:
        """Convert encoder to declination for north side."""
        # Calculate angular distance from nadir
        angle_from_nadir = (encoder - self.dec_encoder_min) / self.dec_steps_deg

        # Convert back to declination
        dec = self.nadir_declination + angle_from_nadir
        return dec

    def _dec_to_encoder_south_side(self, dec: float) -> int:
        """
        Convert declination to encoder when using south side of fork.

        When flipped to south side, the declination reference changes.
        The transformed declination (180° - original_dec) is measured from nadir.
        """
        # Calculate angular distance from nadir for the transformed declination
        angle_from_nadir = dec - self.nadir_declination

        # For south side, we might need to flip the encoder relationship
        # This depends on your specific mount mechanics
        encoder = self.dec_encoder_min + angle_from_nadir * self.dec_steps_deg
        return int(round(encoder))

    def _encoder_to_dec_south_side(self, encoder: int) -> float:
        """Convert encoder to declination for south side."""
        # Calculate angular distance from nadir
        angle_from_nadir = (encoder - self.dec_encoder_min) / self.dec_steps_deg

        # Convert back to declination (for the transformed coordinate)
        dec = self.nadir_declination + angle_from_nadir
        return dec

    def _normalize_ha_24h(self, ha: float) -> float:
        """Normalize HA to [-12, +12] range."""
        while ha > 12.0:
            ha -= 24.0
        while ha < -12.0:
            ha += 24.0
        return ha

    # High-level convenience methods
    def celestial_to_encoders(self, celestial_ha: float, celestial_dec: float) -> Tuple[int, int, ForkSide]:
        """One-step conversion from celestial coordinates to encoders."""
        mount_pos = self.celestial_to_mount(celestial_ha, celestial_dec)
        ha_enc, dec_enc = self.mount_to_encoders(mount_pos)
        return ha_enc, dec_enc, mount_pos.side

    def encoders_to_celestial(self, ha_encoder: int, dec_encoder: int) -> Tuple[float, float, ForkSide]:
        """One-step conversion from encoders to celestial coordinates."""
        mount_pos = self.encoders_to_mount(ha_encoder, dec_encoder)
        celestial_ha, celestial_dec = self.mount_to_celestial(mount_pos)
        return celestial_ha, celestial_dec, mount_pos.side


# Testing and example usage
def test_fork_mount():
    """Test the fork mount coordinate system."""

    # Mock calibration data
    cal_data = {
        'calibrated': True,
        'calibration_date': '2025-07-31T16:07:25.664552',
        'dec_steps_per_degree': 19408,
        'ha_steps_per_degree': 24969.216666666667,
        'limits': {
            'dec_negative': -1534182,
            'dec_positive': 3001074,
            'ha_negative': -2260241,
            'ha_positive': 2234218
        },
        'limits_safety_factor': 0.05,
        'observer_latitude': -25.7479,
        'ranges': {
            'dec_encoder_range': 4535256,
            'ha_encoder_range': 4494459
        },
        'sidereal_rate_ha_steps_per_sec': 104.32325053091577,
        'tracking_safety_buffer_steps': 2500
    }

    coords = ForkMountCoordinates(cal_data)

    # Test cases
    test_cases = [
        (6, -32, "Meridian, north"),
        (3.0, -45.0, "East, north side"),
        (-5.0, 60.0, "West, north side"),
        (8.0, 30.0, "East, south side"),
        (-8.0, -20.0, "West, south side"),
        (11.0, 0.0, "Near meridian, south side"),
    ]

    print("Fork Mount Coordinate System Test")
    print(f"Observer latitude: {cal_data['observer_latitude']}°")
    print(f"Nadir declination: {-(90.0 + abs(cal_data['observer_latitude']))}°")
    print("=" * 50)

    for celestial_ha, celestial_dec, description in test_cases:
        print(f"\n{description}:")
        print(f"  Celestial: HA={celestial_ha:+.1f}h, Dec={celestial_dec:+.1f}°")

        # Forward conversion
        mount_pos = coords.celestial_to_mount(celestial_ha, celestial_dec)
        print(f"  Mount:     HA={mount_pos.ha:+.1f}h, Dec={mount_pos.dec:+.1f}°, Side={mount_pos.side.value}")

        ha_enc, dec_enc = coords.mount_to_encoders(mount_pos)
        print(f"  Encoders:  HA={ha_enc}, Dec={dec_enc}")

        # Round trip test
        mount_back = coords.encoders_to_mount(ha_enc, dec_enc)
        celestial_back = coords.mount_to_celestial(mount_back)

        ha_error = abs(celestial_ha - celestial_back[0])
        dec_error = abs(celestial_dec - celestial_back[1])

        print(f"  Round-trip: HA={celestial_back[0]:+.3f}h, Dec={celestial_back[1]:+.3f}°")
        print(f"  Errors:    HA={ha_error:.6f}h, Dec={dec_error:.6f}°")

        # Check if errors are acceptable for telescope pointing
        # With 100 steps/degree, 1 step = 0.01°, so 0.05° is ~5 steps tolerance
        ha_acceptable = ha_error < 0.01  # ~36 arcseconds
        dec_acceptable = dec_error < 0.05  # ~3 arcminutes

        if ha_acceptable and dec_acceptable:
            print("  ✓ PASS")
        else:
            print("  ✗ FAIL")
            if not ha_acceptable:
                print(f"    HA error {ha_error:.6f}h exceeds 0.01h threshold")
            if not dec_acceptable:
                print(f"    Dec error {dec_error:.6f}° exceeds 0.05° threshold")


if __name__ == "__main__":
    test_fork_mount()