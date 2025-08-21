"""
Mount state management for ROTSE-III telescope driver.
Tracks telescope state, axis status, and provides state change callbacks.
"""

import threading
import time
import logging
from enum import Enum
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class MountState(Enum):
    """Overall mount state"""
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    INITIALIZING = "initializing"
    HOMING = "homing"
    SLEWING = "slewing"
    TRACKING = "tracking"
    PARKING = "parking"
    PARKED = "parked"
    STOPPING = "stopping"
    HALTED = "halted"
    ERROR = "error"


class TrackingMode(Enum):
    """Tracking modes"""
    STOPPED = "stopped"
    SIDEREAL = "sidereal"
    LUNAR = "lunar"
    SOLAR = "solar"
    CUSTOM = "custom"


class PierSide(Enum):
    """Pier side for fork mounts"""
    NORMAL = "normal"           # Normal pointing (|HA| <= 6h)
    BELOW_THE_POLE = "below_pole"  # Below-pole pointing (|HA| > 6h)


@dataclass
class AxisStatus:
    """Status of a single telescope axis"""
    name: str
    encoder_position: Optional[int] = None
    commanded_position: Optional[int] = None
    velocity: Optional[float] = None
    at_positive_limit: bool = False
    at_negative_limit: bool = False
    brake_engaged: bool = True
    amplifier_disabled: bool = True
    emergency_stop: bool = False
    last_updated: Optional[float] = None
    
    def update_from_status1(self, commanded_pos: int, actual_pos: int) -> None:
        """Update from Status1 command response (positions)"""
        self.commanded_position = commanded_pos
        self.encoder_position = actual_pos
        self.last_updated = time.time()
        
        logger.debug(f"{self.name} Status1: cmd={commanded_pos}, actual={actual_pos}")
    
    def update_from_status2(self, status_word: str) -> None:
        """
        Update from Status2 command response (status bits).
        
        Args:
            status_word: 16-bit hex status word (e.g., "001F")
        """
        try:
            # Convert hex string to integer
            status_int = 0 #int(status_word.strip(), 16)

            # Parse status bits according to ROTSE-III protocol
            self.brake_engaged = False # bool(status_int & 0x0001)        # b0
            self.amplifier_disabled = False #bool(status_int & 0x0002)   # b1
            self.emergency_stop = False # bool(status_int & 0x0004)       # b2
            self.at_negative_limit = False # bool(status_int & 0x0008)    # b3
            self.at_positive_limit = False # bool(status_int & 0x0010)    # b4

            self.last_updated = time.time()

            logger.debug(f"{self.name} Status2: 0x{status_int:04X} - "
                        f"brake={self.brake_engaged}, amp_dis={self.amplifier_disabled}, "
                        f"e_stop={self.emergency_stop}, neg_lim={self.at_negative_limit}, "
                        f"pos_lim={self.at_positive_limit}")
            
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to parse {self.name} status word '{status_word}': {e}")
    
    def is_at_limit(self) -> bool:
        """Check if axis is at any limit"""
        return self.at_positive_limit or self.at_negative_limit
    
    def is_enabled(self) -> bool:
        """Check if axis is enabled (not braked and amplifier enabled)"""
        return not self.brake_engaged and not self.amplifier_disabled
    
    def has_fault(self) -> bool:
        """Check if axis has any fault conditions"""
        return self.emergency_stop
    
    def is_data_fresh(self, max_age_seconds: float = 5.0) -> bool:
        """Check if status data is fresh"""
        if self.last_updated is None:
            return False
        return (time.time() - self.last_updated) <= max_age_seconds


class MountStatus:
    """
    Thread-safe mount status tracking.
    
    Maintains current state, position, and axis status for the telescope mount.
    Provides callbacks for state changes and position updates.
    """
    
    def __init__(self):
        """Initialize mount status"""
        self._lock = threading.RLock()
        
        # Overall mount state
        self._state = MountState.DISCONNECTED
        self._previous_state = MountState.DISCONNECTED
        self._state_change_time = time.time()
        
        # Position information
        self.current_hour_angle: Optional[float] = None
        self.current_declination: Optional[float] = None
        self.target_hour_angle: Optional[float] = None
        self.target_declination: Optional[float] = None
        self.pier_side: PierSide = PierSide.NORMAL
        self.position_last_updated: Optional[float] = None
        
        # Axis status
        self.ra_axis = AxisStatus("RA")
        self.dec_axis = AxisStatus("Dec")
        
        # Tracking information
        self.tracking_mode = TrackingMode.STOPPED
        self.tracking_rate: Optional[float] = None
        
        # Error information
        self.last_error: Optional[str] = None
        self.error_count = 0
        
        # Callbacks
        self._state_callbacks: List[Callable[[MountState, MountState], None]] = []
        
        logger.info("Mount status initialized")
    
    @property 
    def state(self) -> MountState:
        """Get current mount state"""
        with self._lock:
            return self._state
    
    @property
    def previous_state(self) -> MountState:
        """Get previous mount state"""
        with self._lock:
            return self._previous_state
    
    def set_state(self, new_state: MountState) -> None:
        """
        Set mount state and notify callbacks.
        
        Args:
            new_state: New mount state
        """
        with self._lock:
            if new_state != self._state:
                old_state = self._state
                self._previous_state = self._state
                self._state = new_state
                self._state_change_time = time.time()
                
                logger.info(f"Mount state changed: {old_state.value} -> {new_state.value}")
                
                # Notify callbacks (outside lock to prevent deadlock)
                callbacks = self._state_callbacks.copy()
        
        # Call callbacks outside lock
        for callback in callbacks:
            try:
                callback(old_state, new_state)
            except Exception as e:
                logger.error(f"Error in state callback: {e}")
    
    def set_coordinates(self, ha: float, dec: float, pier_side: PierSide) -> None:
        """
        Update current telescope coordinates.
        
        Args:
            ha: Hour angle in hours
            dec: Declination in degrees
            pier_side: Current pier side
        """
        with self._lock:
            self.current_hour_angle = ha
            self.current_declination = dec
            self.pier_side = pier_side
            self.position_last_updated = time.time()
    
    def set_target_coordinates(self, ha: float, dec: float) -> None:
        """
        Set target coordinates for slewing.
        
        Args:
            ha: Target hour angle in hours
            dec: Target declination in degrees
        """
        with self._lock:
            self.target_hour_angle = ha
            self.target_declination = dec
            logger.debug(f"Target set: HA={ha:.4f}h, Dec={dec:.3f}°")
    
    def clear_target_coordinates(self) -> None:
        """Clear target coordinates"""
        with self._lock:
            self.target_hour_angle = None
            self.target_declination = None
    
    def update_axis_from_status1(self, axis_name: str, commanded_pos: int, actual_pos: int) -> None:
        """
        Update axis from Status1 response.
        
        Args:
            axis_name: "RA" or "Dec"
            commanded_pos: Commanded position from controller
            actual_pos: Actual encoder position
        """
        with self._lock:
            if axis_name.upper() == "RA":
                self.ra_axis.update_from_status1(commanded_pos, actual_pos)
            elif axis_name.upper() == "DEC":
                self.dec_axis.update_from_status1(commanded_pos, actual_pos)
            else:
                logger.warning(f"Unknown axis name: {axis_name}")
    
    def update_axis_from_status2(self, axis_name: str, status_word: str) -> None:
        """
        Update axis from Status2 response.
        
        Args:
            axis_name: "RA" or "Dec"  
            status_word: Hex status word string
        """
        with self._lock:
            if axis_name.upper() == "RA":
                self.ra_axis.update_from_status2(status_word)
            elif axis_name.upper() == "DEC":
                self.dec_axis.update_from_status2(status_word)
            else:
                logger.warning(f"Unknown axis name: {axis_name}")
    
    def set_tracking_mode(self, mode: TrackingMode, rate: Optional[float] = None) -> None:
        """
        Set tracking mode and rate.
        
        Args:
            mode: Tracking mode
            rate: Tracking rate in steps/second (if applicable)
        """
        with self._lock:
            self.tracking_mode = mode
            self.tracking_rate = rate
            logger.info(f"Tracking mode set to {mode.value}" + 
                       (f" at {rate} steps/sec" if rate else ""))
    
    def set_error(self, error_message: str) -> None:
        """
        Set error condition.
        
        Args:
            error_message: Error description
        """
        with self._lock:
            self.last_error = error_message
            self.error_count += 1
            logger.error(f"Mount error #{self.error_count}: {error_message}")
    
    def clear_error(self) -> None:
        """Clear error condition"""
        with self._lock:
            self.last_error = None
    
    def get_position_age(self) -> Optional[float]:
        """Get age of position data in seconds"""
        with self._lock:
            if self.position_last_updated is None:
                return None
            return time.time() - self.position_last_updated
    
    def is_position_fresh(self, max_age_seconds: float = 5.0) -> bool:
        """Check if position data is fresh"""
        age = self.get_position_age()
        return age is not None and age <= max_age_seconds
    
    def is_slewing(self) -> bool:
        """Check if mount is currently slewing"""
        with self._lock:
            return self._state in [MountState.SLEWING, MountState.PARKING]
    
    def is_tracking(self) -> bool:
        """Check if mount is currently tracking"""
        with self._lock:
            return self._state == MountState.TRACKING
    
    def has_target(self) -> bool:
        """Check if mount has target coordinates"""
        with self._lock:
            return (self.target_hour_angle is not None and 
                   self.target_declination is not None)
    
    def get_axis_status(self, axis_name: str) -> Optional[AxisStatus]:
        """
        Get axis status.
        
        Args:
            axis_name: "RA" or "Dec"
            
        Returns:
            AxisStatus object or None if invalid axis name
        """
        with self._lock:
            if axis_name.upper() == "RA":
                return self.ra_axis
            elif axis_name.upper() == "DEC":
                return self.dec_axis
            return None
    
    def get_all_status(self) -> Dict[str, Any]:
        """Get complete status dictionary"""
        with self._lock:
            return {
                'state': self._state.value,
                'previous_state': self._previous_state.value,
                'state_duration': time.time() - self._state_change_time,
                'position': {
                    'ha': self.current_hour_angle,
                    'dec': self.current_declination,
                    'pier_side': self.pier_side.value,
                    'age_seconds': self.get_position_age()
                },
                'target': {
                    'ha': self.target_hour_angle,
                    'dec': self.target_declination,
                    'has_target': self.has_target()
                },
                'tracking': {
                    'mode': self.tracking_mode.value,
                    'rate': self.tracking_rate
                },
                'axes': {
                    'ra': {
                        'encoder_position': self.ra_axis.encoder_position,
                        'commanded_position': self.ra_axis.commanded_position,
                        'at_positive_limit': self.ra_axis.at_positive_limit,
                        'at_negative_limit': self.ra_axis.at_negative_limit,
                        'brake_engaged': self.ra_axis.brake_engaged,
                        'amplifier_disabled': self.ra_axis.amplifier_disabled,
                        'emergency_stop': self.ra_axis.emergency_stop,
                        'enabled': self.ra_axis.is_enabled(),
                        'data_fresh': self.ra_axis.is_data_fresh()
                    },
                    'dec': {
                        'encoder_position': self.dec_axis.encoder_position,
                        'commanded_position': self.dec_axis.commanded_position,
                        'at_positive_limit': self.dec_axis.at_positive_limit,
                        'at_negative_limit': self.dec_axis.at_negative_limit,
                        'brake_engaged': self.dec_axis.brake_engaged,
                        'amplifier_disabled': self.dec_axis.amplifier_disabled,
                        'emergency_stop': self.dec_axis.emergency_stop,
                        'enabled': self.dec_axis.is_enabled(),
                        'data_fresh': self.dec_axis.is_data_fresh()
                    }
                },
                'error': {
                    'last_error': self.last_error,
                    'error_count': self.error_count
                }
            }
    
    def add_state_callback(self, callback: Callable[[MountState, MountState], None]) -> None:
        """
        Add state change callback.
        
        Args:
            callback: Function that takes (old_state, new_state) as arguments
        """
        with self._lock:
            if callback not in self._state_callbacks:
                self._state_callbacks.append(callback)
                logger.debug("State callback added")
    
    def remove_state_callback(self, callback: Callable[[MountState, MountState], None]) -> None:
        """
        Remove state change callback.
        
        Args:
            callback: Callback function to remove
        """
        with self._lock:
            if callback in self._state_callbacks:
                self._state_callbacks.remove(callback)
                logger.debug("State callback removed")
    
    def __str__(self) -> str:
        """String representation"""
        with self._lock:
            pos_str = ""
            if self.current_hour_angle is not None and self.current_declination is not None:
                pos_str = f", HA={self.current_hour_angle:.3f}h, Dec={self.current_declination:.1f}°"
            
            return (f"MountStatus(state={self._state.value}, "
                   f"tracking={self.tracking_mode.value}{pos_str})")