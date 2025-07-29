from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any, Callable

class MountState(Enum):
    """Telescope mount states"""
    DISCONNECTED = "disconnected"
    INITIALIZING = "initializing"
    IDLE = "idle"
    SLEWING = "slewing"
    TRACKING = "tracking"
    PARKING = "parking"
    PARKED = "parked"
    HOMING = "homing"
    ERROR = "error"
    CALIBRATING = "calibrating"


class TrackingMode(Enum):
    """Different tracking modes"""
    SIDEREAL = "sidereal"
    NON_SIDEREAL = "non_sidereal"
    STOPPED = "stopped"

class PierSide(Enum):
    """Pier side positions for fork equatorial mount"""
    NORMAL = "normal"      # Normal position - telescope on east side of pier
    BELOW_THE_POLE = "below_the_pole"      # Flipped position - telescope on west side of pier
    UNKNOWN = "unknown"

@dataclass
class MountStatus:
    """Current mount status and position"""
    state: MountState = MountState.DISCONNECTED

    ra_encoder: Optional[int] = None
    dec_encoder: Optional[int] = None
    current_hour_angle: Optional[float] = None
    current_declination: Optional[float] = None

    target_ra_encoder: Optional[int] = None
    target_dec_encoder: Optional[int] = None
    target_hour_angle: Optional[float] = None
    target_declination: Optional[float] = None


    tracking_mode: TrackingMode = TrackingMode.STOPPED
    is_moving: bool = False

    last_position_update: float = 0
    slew_start_time: Optional[float] = None

    # Pier side information
    pier_side: PierSide = PierSide.UNKNOWN


    # Add callbacks for state change notifications
    state_callbacks: list = field(default_factory=list)

