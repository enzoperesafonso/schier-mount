"""
Mount state definitions and status tracking for ROTSE-III telescope driver.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any, Callable
import time
import threading
import logging

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
    HALTED = "halted"  # Mount is halted (brakes on, amplifiers disabled)
    STOPPING = "stopping"  # In the process of stopping

class TrackingMode(Enum):
    """Different tracking modes"""
    SIDEREAL = "sidereal"
    NON_SIDEREAL = "non_sidereal"
    STOPPED = "stopped"

class PierSide(Enum):
    """Pier side positions for fork equatorial mount"""
    NORMAL = "normal"  # Normal position - |HA| <= 6h
    BELOW_THE_POLE = "below_the_pole"  # Flipped position - |HA| > 6h
    UNKNOWN = "unknown"

class AxisState(Enum):
    """Individual axis states based on status word bits"""
    IDLE = "idle"
    MOVING = "moving"
    HALTED = "halted"
    LIMIT_NEGATIVE = "limit_negative"
    LIMIT_POSITIVE = "limit_positive"
    EMERGENCY_STOP = "emergency_stop"
    FAULT = "fault"
    ERROR = "error"

@dataclass
class AxisStatus:
    """Status for individual axis (RA or Dec)"""
    encoder_position: Optional[int] = None
    target_position: Optional[int] = None
    velocity: Optional[int] = None
    state: AxisState = AxisState.IDLE

    # Status word bits
    brake_engaged: bool = True
    amplifier_disabled: bool = True
    in_emergency_stop: bool = False
    in_negative_limit: bool = False
    in_positive_limit: bool = False

    # Status3 values
    amplifier_drive_signal: Optional[int] = None
    integrator_value: Optional[int] = None

    # Fault information
    last_fault: Optional[str] = None
    fault_timestamp: Optional[float] = None

@dataclass
class MountStatus:
    """Current mount status and position - thread-safe"""

    # Core state
    state: MountState = MountState.DISCONNECTED
    tracking_mode: TrackingMode = TrackingMode.STOPPED
    pier_side: PierSide = PierSide.UNKNOWN
    is_moving: bool = False

    # Individual axis status
    ra_axis: AxisStatus = field(default_factory=AxisStatus)
    dec_axis: AxisStatus = field(default_factory=AxisStatus)

    # Coordinate information
    current_hour_angle: Optional[float] = None
    current_declination: Optional[float] = None
    target_hour_angle: Optional[float] = None
    target_declination: Optional[float] = None

    # Timing information
    last_position_update: float = 0
    slew_start_time: Optional[float] = None

    # Thread safety
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # Callbacks for state change notifications
    state_callbacks: list = field(default_factory=list)

    def __post_init__(self):
        """Initialize after dataclass creation"""
        self.last_position_update = time.time()

    def update_axis_from_status2(self, axis: str, status_response: str) -> None:
        """Update axis status from Status2 response (16-bit hex word, possibly comma-separated)"""
        with self._lock:
            try:
                # Handle comma-separated responses - extract first value (status word)
                if ',' in status_response:
                    status_word = status_response.split(',')[0].strip()
                else:
                    status_word = status_response.strip()

                # Handle invalid/empty responses gracefully
                if not status_word or status_word == '':
                    logger = logging.getLogger(__name__)
                    logger.warning(f"Received empty status word for {axis}: '{status_word}' - telescope may not be responding")
                    # Set to error state but don't crash
                    axis_status = self.ra_axis if axis.upper() == 'RA' else self.dec_axis
                    axis_status.state = AxisState.ERROR
                    return

                status_int = int(status_word, 16)
                axis_status = self.ra_axis if axis.upper() == 'RA' else self.dec_axis

                # Parse status bits (b15...b0)
                axis_status.brake_engaged = bool(status_int & 0x01)  # b0
                axis_status.amplifier_disabled = bool(status_int & 0x02)  # b1
                axis_status.in_emergency_stop = bool(status_int & 0x04)  # b2
                axis_status.in_negative_limit = bool(status_int & 0x08)  # b3
                axis_status.in_positive_limit = bool(status_int & 0x10)  # b4

                # Determine axis state
                if axis_status.in_emergency_stop:
                    axis_status.state = AxisState.EMERGENCY_STOP
                elif axis_status.in_negative_limit:
                    axis_status.state = AxisState.LIMIT_NEGATIVE
                elif axis_status.in_positive_limit:
                    axis_status.state = AxisState.LIMIT_POSITIVE
                elif axis_status.brake_engaged and axis_status.amplifier_disabled:
                    axis_status.state = AxisState.HALTED
                elif not axis_status.brake_engaged and not axis_status.amplifier_disabled:
                    axis_status.state = AxisState.MOVING
                else:
                    axis_status.state = AxisState.IDLE

            except (ValueError, TypeError) as e:
                logger = logging.getLogger(__name__)
                logger.warning(f"Error parsing status word for {axis}: {status_word} - {e}")
                # Set to error state but don't crash the driver
                axis_status = self.ra_axis if axis.upper() == 'RA' else self.dec_axis
                axis_status.state = AxisState.ERROR

    def update_axis_from_status1(self, axis: str, command_pos: int, actual_pos: int) -> None:
        """Update axis status from Status1 response (positions)"""
        with self._lock:
            axis_status = self.ra_axis if axis.upper() == 'RA' else self.dec_axis
            axis_status.target_position = command_pos
            axis_status.encoder_position = actual_pos
            self.last_position_update = time.time()

    def update_axis_from_status3(self, axis: str, drive_signal: int, integrator: int) -> None:
        """Update axis status from Status3 response"""
        with self._lock:
            axis_status = self.ra_axis if axis.upper() == 'RA' else self.dec_axis
            axis_status.amplifier_drive_signal = drive_signal
            axis_status.integrator_value = integrator

    def set_coordinates(self, ha: float, dec: float, pier_side: PierSide) -> None:
        """Update current coordinates (thread-safe)"""
        with self._lock:
            self.current_hour_angle = ha
            self.current_declination = dec
            self.pier_side = pier_side
            self.last_position_update = time.time()

    def set_target_coordinates(self, ha: float, dec: float) -> None:
        """Set target coordinates (thread-safe)"""
        with self._lock:
            self.target_hour_angle = ha
            self.target_declination = dec

    def set_state(self, new_state: MountState) -> None:
        """Change mount state and notify callbacks (thread-safe)"""
        with self._lock:
            old_state = self.state
            self.state = new_state

            # Update related flags
            if new_state == MountState.SLEWING:
                self.is_moving = True
                if self.slew_start_time is None:
                    self.slew_start_time = time.time()
            elif new_state in [MountState.IDLE, MountState.TRACKING, MountState.PARKED]:
                self.is_moving = False
                self.slew_start_time = None

            # Notify callbacks
            for callback in self.state_callbacks:
                try:
                    callback(old_state, new_state)
                except Exception as e:
                    # Don't let callback errors break state changes
                    pass

    def add_state_callback(self, callback: Callable[[MountState, MountState], None]) -> None:
        """Add callback for state changes"""
        with self._lock:
            self.state_callbacks.append(callback)

    def remove_state_callback(self, callback: Callable[[MountState, MountState], None]) -> None:
        """Remove state change callback"""
        with self._lock:
            if callback in self.state_callbacks:
                self.state_callbacks.remove(callback)

    def is_axis_in_fault(self, axis: str) -> bool:
        """Check if axis is in fault condition"""
        with self._lock:
            axis_status = self.ra_axis if axis.upper() == 'RA' else self.dec_axis
            return axis_status.state in [
                AxisState.EMERGENCY_STOP,
                AxisState.LIMIT_NEGATIVE,
                AxisState.LIMIT_POSITIVE,
                AxisState.FAULT,
                AxisState.ERROR
            ]
    
    def is_any_axis_in_fault(self) -> bool:
        """Check if any axis is in fault condition"""
        return self.is_axis_in_fault('RA') or self.is_axis_in_fault('DEC')
    
    def get_position_age(self) -> float:
        """Get age of last position update in seconds"""
        return time.time() - self.last_position_update
    
    def get_slew_duration(self) -> Optional[float]:
        """Get duration of current slew in seconds"""
        if self.slew_start_time is None:
            return None
        return time.time() - self.slew_start_time
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert status to dictionary for serialization (thread-safe)"""
        with self._lock:
            return {
                'state': self.state.value,
                'tracking_mode': self.tracking_mode.value,
                'pier_side': self.pier_side.value,
                'is_moving': self.is_moving,
                'coordinates': {
                    'current_ha': self.current_hour_angle,
                    'current_dec': self.current_declination,
                    'target_ha': self.target_hour_angle,
                    'target_dec': self.target_declination
                },
                'ra_axis': {
                    'encoder_position': self.ra_axis.encoder_position,
                    'target_position': self.ra_axis.target_position,
                    'state': self.ra_axis.state.value,
                    'brake_engaged': self.ra_axis.brake_engaged,
                    'amplifier_disabled': self.ra_axis.amplifier_disabled,
                    'in_emergency_stop': self.ra_axis.in_emergency_stop,
                    'in_negative_limit': self.ra_axis.in_negative_limit,
                    'in_positive_limit': self.ra_axis.in_positive_limit
                },
                'dec_axis': {
                    'encoder_position': self.dec_axis.encoder_position,
                    'target_position': self.dec_axis.target_position,
                    'state': self.dec_axis.state.value,
                    'brake_engaged': self.dec_axis.brake_engaged,
                    'amplifier_disabled': self.dec_axis.amplifier_disabled,
                    'in_emergency_stop': self.dec_axis.in_emergency_stop,
                    'in_negative_limit': self.dec_axis.in_negative_limit,
                    'in_positive_limit': self.dec_axis.in_positive_limit
                },
                'timing': {
                    'last_position_update': self.last_position_update,
                    'position_age': self.get_position_age(),
                    'slew_start_time': self.slew_start_time,
                    'slew_duration': self.get_slew_duration()
                }
            }