"""
Complete, robust, thread-safe telescope driver for ROTSE-III mount.
Provides high-level interface for telescope control with continuous monitoring.
"""

import threading
import time
import logging
import json
import math
from typing import Optional, Tuple, Dict, Any, List, Callable
from pathlib import Path
from dataclasses import dataclass
from enum import Enum

from .communication import TelescopeSerial
from .coordinates import Coordinates
from .state import MountStatus, MountState, TrackingMode, PierSide, AxisState

logger = logging.getLogger(__name__)

@dataclass
class CalibrationData:
    """Mount calibration parameters"""
    observer_latitude: float
    limits: Dict[str, int]
    ranges: Dict[str, int]
    dec_steps_per_degree: float
    ra_steps_per_degree: float

    @classmethod
    def load_from_file(cls, filepath: Path) -> 'CalibrationData':
        """Load calibration from JSON file"""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(**data)

    def save_to_file(self, filepath: Path) -> None:
        """Save calibration to JSON file"""
        with open(filepath, 'w') as f:
            json.dump(self.__dict__, f, indent=2)

class SlewMode(Enum):
    """Slewing behavior modes"""
    FAST = "fast"          # Maximum speed slew
    NORMAL = "normal"      # Normal speed with acceleration limits
    PRECISE = "precise"    # Slow, precise positioning

class TelescopeDriver:
    """
    Main telescope driver class providing complete mount control.

    Features:
    - Thread-safe operations
    - Continuous status monitoring
    - Robust error handling and recovery
    - HA/Dec coordinate slewing
    - Tracking state management
    - Position monitoring and logging
    """

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600,
                 calibration_data: Optional[CalibrationData] = None,
                 calibration_file: Optional[Path] = None,
                 safe_position: Optional[Dict[str, float]] = None):

        # Core components
        self.status = MountStatus()
        self.serial = TelescopeSerial(device, baudrate)

        # Load calibration data (prioritize passed data over file)
        if calibration_data:
            self.calibration = calibration_data
            logger.info("Using provided calibration data")
        elif calibration_file and calibration_file.exists():
            self.calibration = CalibrationData.load_from_file(calibration_file)
            logger.info(f"Loaded calibration from {calibration_file}")
        else:
            # Default calibration (approximate)
            self.calibration = CalibrationData(
                observer_latitude=-23.2716,  # hess latitude
                limits={
                    'ha_positive': 3447618,
                    'dec_negative': -1560846,
                },
                ranges={
                    'ha_encoder_range': 4497505,
                    'dec_encoder_range': 4535809
                },
                dec_steps_per_degree= 19408.0,  # Approximate
                ra_steps_per_degree= 100000
            )
            logger.warning("Using default calibration data - should be replaced with actual values")

        self.coordinates = Coordinates(self.status, self.calibration.__dict__)

        # Safe position after homing - use a position close to the actual home position
        # This minimizes movement after homing to prevent overshoot issues
        self.safe_position = safe_position or {'ha': 0.0, 'dec': -30.0}

        # Thread management
        self._monitoring_thread: Optional[threading.Thread] = None
        self._monitoring_active = False
        self._shutdown_event = threading.Event()
        self._command_lock = threading.RLock()

        # Motion parameters
        self.slew_parameters = {
            SlewMode.FAST: {       # for when telescope needs to go ZOOOOOOOM....
                'ra_velocity': 50000,
                'dec_velocity': 25000,  # Reduced Dec velocity for better control
                'ra_acceleration': 10000,
                'dec_acceleration': 5000  # Reduced Dec acceleration
            },
            SlewMode.NORMAL: {
                'ra_velocity': 30000,
                'dec_velocity': 15000,  # Further reduced Dec velocity
                'ra_acceleration': 3000,  # Reduced from 5000 for less overshoot
                'dec_acceleration': 1000  # Much more conservative for Dec axis precision
            },
            SlewMode.PRECISE: {
                'ra_velocity': 5000,
                'dec_velocity': 3000,   # Very conservative Dec velocity
                'ra_acceleration': 1000,
                'dec_acceleration': 300   # Very precise for Dec axis
            }
        }
        
        # Special initialization parameters for post-homing movements
        self.initialization_parameters = {
            'ra_velocity': 15000,        # Moderate speed for safety
            'dec_velocity': 10000,       # Slower Dec for precision
            'ra_acceleration': 2000,     # Conservative acceleration
            'dec_acceleration': 1000     # Very conservative Dec acceleration
        }

        # Monitoring configuration
        self.monitoring_interval = 1.0  # seconds
        self.position_tolerance = 5000    # encoder steps - increased for Dec axis issues
        self.initialization_tolerance = 2000  # More forgiving tolerance for initialization moves
        self.slew_timeout = 300.0       # seconds - increased timeout for slower Dec motion

        # Event callbacks
        self._position_callbacks: List[Callable] = []
        self._error_callbacks: List[Callable] = []

        # Commanded target positions (what we told the telescope to go to)
        self._commanded_ra_target: Optional[int] = None
        self._commanded_dec_target: Optional[int] = None

        # Home encoder positions (captured immediately after homing)
        self._home_ha_position: Optional[int] = None
        self._home_dec_position: Optional[int] = None

        # Parking mode flag - when True, slew completion goes to PARKED instead of IDLE
        self._parking_mode: bool = False

        logger.info("TelescopeDriver initialized")

    def connect(self) -> bool:
        """Connect to telescope and initialize"""
        logger.info("Connecting to telescope")

        try:
            # Connect serial communication
            if not self.serial.connect():
                logger.error("Failed to connect to serial port")
                return False

            self.status.set_state(MountState.INITIALIZING)

            # Initial status check
            self._update_status()

            # Start monitoring thread
            self._start_monitoring()

            self.status.set_state(MountState.IDLE)
            logger.info("Telescope connected successfully")
            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.status.set_state(MountState.ERROR)
            return False

    def disconnect(self) -> None:
        """Disconnect from telescope and cleanup"""
        logger.info("Disconnecting telescope")

        # Stop all telescope motion before telescope disconnect
        self.stop()


        self.status.set_state(MountState.DISCONNECTED)
        self._shutdown_event.set()

        # Stop monitoring
        self._stop_monitoring()

        # Disconnect serial
        self.serial.disconnect()

        logger.info("Telescope disconnected")

    def _start_monitoring(self) -> None:
        """Start background monitoring thread"""
        if self._monitoring_thread is None or not self._monitoring_thread.is_alive():
            self._monitoring_active = True
            self._monitoring_thread = threading.Thread(
                target=self._monitoring_loop,
                name="telescope_monitor",
                daemon=True
            )
            self._monitoring_thread.start()
            logger.info("Monitoring thread started")

    def _stop_monitoring(self) -> None:
        """Stop monitoring thread"""
        self._monitoring_active = False
        if self._monitoring_thread and self._monitoring_thread.is_alive():
            self._monitoring_thread.join(timeout=5.0)
            if self._monitoring_thread.is_alive():
                logger.warning("Monitoring thread did not shut down gracefully")

    def _monitoring_loop(self) -> None:
        """Main monitoring loop - runs continuously"""
        logger.info("Monitoring loop started")

        while self._monitoring_active and not self._shutdown_event.is_set():
            try:
                # Update telescope status
                self._update_status()

                # Check for slew completion
                if self.status.state in [MountState.SLEWING, MountState.PARKING]:
                    # Debug logging for parking state tracking
                    if self.status.state == MountState.PARKING:
                        logger.debug(f"Monitoring: In PARKING state, parking_mode={self._parking_mode}")
                    self._check_slew_completion()

                # Check for faults
                self._check_faults()

                # Notify position callbacks
                self._notify_position_callbacks()

                # Sleep until next update
                time.sleep(self.monitoring_interval)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                self._notify_error_callbacks(e)
                time.sleep(self.monitoring_interval)

        logger.info("Monitoring loop stopped")

    def _update_status(self) -> None:
        """Update telescope status from mount"""
        try:
            with self._command_lock:
                # Get encoder positions
                ra_response = self.serial.send_command("$Status1RA", "@Status1RA", timeout=3.0)
                dec_response = self.serial.send_command("$Status1Dec", "@Status1Dec", timeout=3.0)

                # Parse encoder positions
                if ra_response and "," in ra_response:
                    parts = ra_response.split(",")
                    if len(parts) >= 2:
                        ra_cmd_pos = int(float(parts[0].strip()))
                        ra_actual_pos = int(float(parts[1].strip()))
                        self.status.update_axis_from_status1("RA", ra_cmd_pos, ra_actual_pos)

                if dec_response and "," in dec_response:
                    parts = dec_response.split(",")
                    if len(parts) >= 2:
                        dec_cmd_pos = int(float(parts[0].strip()))
                        dec_actual_pos = int(float(parts[1].strip()))
                        self.status.update_axis_from_status1("DEC", dec_cmd_pos, dec_actual_pos)

                # Get status words
                ra_status2 = self.serial.send_command("$Status2RA", "@Status2RA", timeout=3.0)
                dec_status2 = self.serial.send_command("$Status2Dec", "@Status2Dec", timeout=3.0)

                if ra_status2:
                    self.status.update_axis_from_status2("RA", ra_status2)
                if dec_status2:
                    self.status.update_axis_from_status2("DEC", dec_status2)

                # Update coordinates if we have encoder positions
                if (self.status.ra_axis.encoder_position is not None and
                    self.status.dec_axis.encoder_position is not None):

                    ha, dec, below_pole = self.coordinates.encoder_positions_to_ha_dec(
                        self.status.ra_axis.encoder_position,
                        self.status.dec_axis.encoder_position
                    )

                    pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
                    self.status.set_coordinates(ha, dec, pier_side)

        except Exception as e:
            logger.error(f"Error updating status: {e}")
            raise

    def _check_slew_completion(self) -> None:
        """Check if slew has completed - using position-only approach like old implementation"""
        if self.status.state not in [MountState.SLEWING, MountState.PARKING]:
            return

        # Check if both axes are at target position
        ra_at_target = self._is_axis_at_target("RA")
        dec_at_target = self._is_axis_at_target("DEC")

        # Debug logging
        logger.debug(f"Slew completion check - RA: at_target={ra_at_target}, Dec: at_target={dec_at_target}")

        # Simple position-based completion (like old implementation)
        if ra_at_target and dec_at_target:
            logger.info("Slew completed successfully - both axes at target position")

            # Send stop commands to ensure axes stop at target
            try:
                with self._command_lock:
                    self.serial.send_command("$StopRA", "@StopRA", priority=10)
                    self.serial.send_command("$StopDec", "@StopDec", priority=10)
                    logger.info("Sent stop commands to both axes")

                    # Give a moment for stop commands to take effect
                    time.sleep(0.2)
            except Exception as e:
                logger.error(f"Error sending final stop commands: {e}")

            # Mark slew as complete - check if we're in parking mode
            logger.info(f"Slew completion check: parking_mode={self._parking_mode}, current_state={self.status.state}")
            
            if self._parking_mode:
                logger.info("PARKING MODE DETECTED - Setting state to PARKED")
                self.status.set_state(MountState.PARKED)
                self._parking_mode = False  # Reset parking mode
                logger.info("✓ Parking completed successfully - telescope parked")
            else:
                logger.info("Normal slew mode - setting state to IDLE")
                self.status.set_state(MountState.IDLE)
                logger.info("✓ Slew completed successfully - telescope idle")

        elif self.status.get_slew_duration() and self.status.get_slew_duration() > self.slew_timeout:
            logger.error("Slew timeout exceeded")
            self.emergency_stop()
            self.status.set_state(MountState.ERROR)

    def _is_axis_at_target(self, axis: str) -> bool:
        """Check if axis is at target position within tolerance using driver-commanded positions"""
        axis_status = self.status.ra_axis if axis.upper() == "RA" else self.status.dec_axis
        commanded_target = self._commanded_ra_target if axis.upper() == "RA" else self._commanded_dec_target

        if axis_status.encoder_position is None or commanded_target is None:
            logger.debug(f"{axis} axis - missing position data: current={axis_status.encoder_position}, commanded_target={commanded_target}")
            return False

        error = abs(axis_status.encoder_position - commanded_target)
        at_target = error <= self.position_tolerance
        
        # Enhanced logging for Dec axis to diagnose overshoot
        if axis.upper() == "DEC":
            overshoot = axis_status.encoder_position - commanded_target
            if abs(overshoot) > self.position_tolerance:
                logger.warning(f"Dec axis overshoot detected: current={axis_status.encoder_position}, target={commanded_target}, overshoot={overshoot} steps")
        
        logger.debug(f"{axis} axis - current={axis_status.encoder_position}, target={commanded_target}, error={error} steps, tolerance={self.position_tolerance}, at_target={at_target}")

        return at_target

    def _check_faults(self) -> None:
        """Check for fault conditions"""

        # Special handling for tracking into limits
        if self.status.state == MountState.TRACKING:
            if self.status.ra_axis.state in [AxisState.LIMIT_NEGATIVE, AxisState.LIMIT_POSITIVE]:
                logger.warning("Telescope tracked into HA limit - stopping tracking for safety")
                self._handle_tracking_limit()
                return

        # Standard fault detection
        if self.status.is_any_axis_in_fault():
            logger.error("Fault detected in telescope axes")
            if self.status.state not in [MountState.ERROR, MountState.HALTED]:
                self.status.set_state(MountState.ERROR)

    def _notify_position_callbacks(self) -> None:
        """Notify position update callbacks"""
        for callback in self._position_callbacks:
            try:
                callback(self.status.current_hour_angle, self.status.current_declination)
            except Exception as e:
                logger.error(f"Error in position callback: {e}")

    def _notify_error_callbacks(self, error: Exception) -> None:
        """Notify error callbacks"""
        for callback in self._error_callbacks:
            try:
                callback(error)
            except Exception as e:
                logger.error(f"Error in error callback: {e}")

    def _handle_tracking_limit(self) -> None:
        """Handle telescope tracking into a limit switch"""
        try:
            with self._command_lock:
                logger.warning("Tracking limit reached - implementing safety response")

                # Immediately stop both axes for safety
                self.serial.send_command("$StopRA", "@StopRA", priority=10, timeout=2.0)
                self.serial.send_command("$StopDec", "@StopDec", priority=10, timeout=2.0)

                # Stop tracking mode
                self.status.tracking_mode = TrackingMode.STOPPED
                self.status.set_state(MountState.IDLE)

                # Move slightly away from limit for safety
                current_ha = self.status.current_hour_angle
                if current_ha is not None:
                    if self.status.ra_axis.state == AxisState.LIMIT_NEGATIVE:
                        # Hit negative limit, move positive by 30 arcminutes (0.5°)
                        safe_ha = current_ha + 0.5/15.0  # Convert degrees to hours
                        logger.info("Hit negative HA limit - moving 30 arcmin toward positive side")
                    else:  # LIMIT_POSITIVE
                        # Hit positive limit, move negative by 30 arcminutes
                        safe_ha = current_ha - 0.5/15.0  # Convert degrees to hours
                        logger.info("Hit positive HA limit - moving 30 arcmin toward negative side")

                    current_dec = self.status.current_declination or 0.0

                    # Small recovery slew away from limit
                    if self.slew_to_ha_dec(safe_ha, current_dec, SlewMode.PRECISE):
                        logger.info(f"Initiated recovery slew to HA={safe_ha:.3f}h, Dec={current_dec:.1f}°")
                    else:
                        logger.error("Failed to initiate recovery slew - telescope may be stuck at limit")
                        self.status.set_state(MountState.ERROR)
                else:
                    logger.error("Cannot determine recovery position - current HA unknown")
                    self.status.set_state(MountState.ERROR)

        except Exception as e:
            logger.error(f"Error handling tracking limit: {e}")
            self.status.set_state(MountState.ERROR)

    # Public interface methods

    def initialization_slew_to_ha_dec(self, ha: float, dec: float) -> bool:
        """
        Special slew method for post-initialization movements.
        Uses conservative motion parameters to prevent overshoot.
        
        Args:
            ha: Hour angle in hours
            dec: Declination in degrees
            
        Returns:
            True if slew was initiated successfully
        """
        logger.info(f"Initialization slew to HA={ha:.3f}h, Dec={dec:.1f}° (conservative parameters)")
        
        try:
            with self._command_lock:
                # Check if position is reachable
                if not self.coordinates.is_position_reachable(ha, dec):
                    logger.error(f"Position HA={ha:.3f}h, Dec={dec:.1f}° is not reachable")
                    return False

                # Convert to encoder positions
                ha_enc, dec_enc, below_pole = self.coordinates.ha_dec_to_encoder_positions(ha, dec)

                # Set target coordinates
                self.status.set_target_coordinates(ha, dec)

                # Use conservative initialization parameters
                self._set_motion_parameters(self.initialization_parameters)

                # Execute slew with tighter tolerance
                old_tolerance = self.position_tolerance
                self.position_tolerance = self.initialization_tolerance
                
                try:
                    success = self._slew_to_encoder_positions(ha_enc, dec_enc)
                    if success:
                        self.status.set_state(MountState.SLEWING)
                        pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
                        logger.info(f"Initialization slew initiated: RA={ha_enc}, Dec={dec_enc}, pier_side={pier_side.value}")
                    else:
                        logger.error("Failed to initiate initialization slew")
                finally:
                    # Restore normal tolerance
                    self.position_tolerance = old_tolerance

                return success

        except Exception as e:
            logger.error(f"Error during initialization slew: {e}")
            self.status.set_state(MountState.ERROR)
            return False

    def slew_to_ha_dec(self, ha: float, dec: float, mode: SlewMode = SlewMode.NORMAL) -> bool:
        """
        Slew telescope to specified HA/Dec coordinates

        Args:
            ha: Hour angle in hours
            dec: Declination in degrees
            mode: Slewing speed/behavior mode

        Returns:
            True if slew was initiated successfully
        """
        logger.info(f"Slewing to HA={ha:.3f}h, Dec={dec:.1f}° in {mode.value} mode")

        try:
            with self._command_lock:
                # Check if position is reachable
                if not self.coordinates.is_position_reachable(ha, dec):
                    logger.error(f"Position HA={ha:.3f}h, Dec={dec:.1f}° is not reachable")
                    return False

                # Convert to encoder positions
                ha_enc, dec_enc, below_pole = self.coordinates.ha_dec_to_encoder_positions(ha, dec)
                
                # Additional safety check for encoder limits
                if not (self.calibration.limits['ha_negative'] <= ha_enc <= self.calibration.limits['ha_positive']):
                    logger.error(f"HA encoder position {ha_enc} outside limits [{self.calibration.limits['ha_negative']}, {self.calibration.limits['ha_positive']}]")
                    return False
                    
                if not (self.calibration.limits['dec_negative'] <= dec_enc <= self.calibration.limits['dec_positive']):
                    logger.error(f"Dec encoder position {dec_enc} outside limits [{self.calibration.limits['dec_negative']}, {self.calibration.limits['dec_positive']}]")
                    return False

                # Set target coordinates
                self.status.set_target_coordinates(ha, dec)

                # Set motion parameters
                params = self.slew_parameters[mode]
                self._set_motion_parameters(params)

                # Execute slew
                success = self._slew_to_encoder_positions(ha_enc, dec_enc)

                if success:
                    # Check if we're in parking mode - if so, keep PARKING state instead of SLEWING
                    if not self._parking_mode:
                        self.status.set_state(MountState.SLEWING)
                    # If parking mode, state should already be PARKING, so don't change it

                    pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
                    state_name = "parking" if self._parking_mode else "slewing"
                    logger.info(f"{state_name.capitalize()} initiated to encoders RA={ha_enc}, Dec={dec_enc}, pier_side={pier_side.value}")
                else:
                    logger.error("Failed to initiate slew")

                return success

        except Exception as e:
            logger.error(f"Error during slew: {e}")
            self.status.set_state(MountState.ERROR)
            return False

    def _set_motion_parameters(self, params: Dict[str, int]) -> None:
        """Set motion parameters for slewing"""
        try:
            # Set velocities
            self.serial.send_command(f"$VelRA {params['ra_velocity']}", "@VelRA")
            self.serial.send_command(f"$VelDec {params['dec_velocity']}", "@VelDec")

            # Set accelerations
            self.serial.send_command(f"$AccelRA {params['ra_acceleration']}", "@AccelRA")
            self.serial.send_command(f"$AccelDec {params['dec_acceleration']}", "@AccelDec")

            logger.debug(f"Motion parameters set: {params}")

        except Exception as e:
            logger.error(f"Error setting motion parameters: {e}")
            raise

    def _slew_to_encoder_positions(self, ra_enc: int, dec_enc: int) -> bool:
        """Execute low-level slew to encoder positions"""
        try:
            # CRITICAL: Stop both axes first with timeouts to prevent freezing
            logger.debug("Stopping both axes before slew with timeouts")
            self.serial.send_command("$StopRA", "@StopRA", priority=10, timeout=3.0)
            self.serial.send_command("$StopDec", "@StopDec", priority=10, timeout=3.0)

            # Wait for stop commands to take effect
            time.sleep(0.5)
            logger.debug("Stop commands completed, proceeding with slew")

            # Store commanded target positions (what we're commanding the telescope to go to)
            self._commanded_ra_target = ra_enc
            self._commanded_dec_target = dec_enc
            logger.info(f"Set commanded target positions: RA={ra_enc}, Dec={dec_enc}")

            # Set target positions on telescope
            self.serial.send_command(f"$PosRA {ra_enc}", "@PosRA", timeout=3.0)
            self.serial.send_command(f"$PosDec {dec_enc}", "@PosDec", timeout=3.0)

            # Start motion with timeouts
            logger.debug("Starting motion on both axes")
            self.serial.send_command("$RunRA", "@RunRA", timeout=3.0)
            self.serial.send_command("$RunDec", "@RunDec", timeout=3.0)

            logger.info("Slew commands sent successfully")
            return True

        except Exception as e:
            logger.error(f"Error executing slew: {e}")
            return False

    def stop(self) -> bool:
        """Stop telescope motion gracefully"""
        logger.info("Stopping telescope motion")

        try:
            with self._command_lock:
                self.status.set_state(MountState.STOPPING)

                # Send stop commands
                self.serial.send_command("$StopRA", "@StopRA", priority=100, timeout=3.0)
                self.serial.send_command("$StopDec", "@StopDec", priority=100, timeout=3.0)

                # Wait a moment for stop to take effect
                time.sleep(0.5)

                self.status.set_state(MountState.IDLE)
                logger.info("Telescope stopped successfully")
                return True

        except Exception as e:
            logger.error(f"Error stopping telescope: {e}")
            self.status.set_state(MountState.ERROR)
            return False

    def emergency_stop(self) -> bool:
        """Emergency halt - immediate stop with brakes"""
        logger.warning("Emergency stop activated")

        try:
            # Send halt commands (highest priority)
            self.serial.send_command("StopRA", "@StopRA", priority=100, timeout=2.0, retries=1)
            self.serial.send_command("StopRA", "@StopRA", priority=100, timeout=2.0, retries=1)

            self.status.set_state(MountState.HALTED)
            logger.info("Emergency stop completed")
            return True

        except Exception as e:
            logger.error(f"Error during emergency stop: {e}")
            return False

    def home(self) -> bool:
        """Home telescope to index position"""
        logger.info("Homing telescope")

        try:
            with self._command_lock:
                self.status.set_state(MountState.HOMING)

                # Stop first, then home
                logger.debug("Stopping both axes before homing")
                self.serial.send_command("$StopRA", "@StopRA", timeout=3.0)
                self.serial.send_command("$StopDec", "@StopDec", timeout=3.0)

                # Wait for stop to complete
                time.sleep(1.0)

                # Execute homing sequence
                logger.debug("Starting homing sequence")
                self.serial.send_command("$HomeRA", "@HomeRA", timeout=30.0)
                self.serial.send_command("$HomeDec", "@HomeDec", timeout=30.0)

                # Wait for homing to complete, then update status
                time.sleep(100.0)
                self._update_status()

                # CRITICAL: Capture encoder positions immediately after homing, before ANY movement
                logger.info("Capturing encoder positions immediately after homing for limit calibration")
                self._capture_home_positions()

                # Calculate a safe position that minimizes movement from current home position
                safe_ha, safe_dec = self._calculate_safe_position_from_home()
                logger.info(f"Moving to safe position after homing (HA={safe_ha:.3f}h, Dec={safe_dec:.1f}°)")
                logger.info("Using conservative initialization parameters to prevent overshoot")

                if not self.initialization_slew_to_ha_dec(safe_ha, safe_dec):
                    logger.warning("Failed to move to safe position after homing, but homing was successful")
                else:
                    # Wait for slew to complete with shorter check intervals for precise positioning
                    while self.status.state == MountState.SLEWING:
                        time.sleep(0.2)  # More frequent checks for precise positioning
                    logger.info("Moved to safe position after homing with precise positioning")

                self.status.set_state(MountState.IDLE)
                logger.info("Homing and positioning completed")
                return True

        except Exception as e:
            logger.error(f"Error during homing: {e}")
            self.status.set_state(MountState.ERROR)
            return False

    def _capture_home_positions(self) -> None:
        """Capture encoder positions immediately after homing for auto-calibration."""
        try:
            logger.info("Reading encoder positions at limit switches for calibration...")
            
            # Get current encoder positions (these should be the home positions at limits)
            ha_pos = self.status.ra_axis.encoder_position
            dec_pos = self.status.dec_axis.encoder_position

            if ha_pos is not None and dec_pos is not None:
                # Store the home positions for later retrieval
                self._home_ha_position = ha_pos
                self._home_dec_position = dec_pos

                logger.info(f"✓ Successfully captured home limit positions:")
                logger.info(f"  HA at positive limit: {ha_pos}")
                logger.info(f"  Dec at negative limit: {dec_pos}")
                logger.info("These positions will be used to calculate all telescope limits")
            else:
                logger.warning("✗ Could not capture home positions - encoder positions unavailable")
                logger.warning("Auto-calibration will use config estimates instead of measured positions")
                self._home_ha_position = None
                self._home_dec_position = None

        except Exception as e:
            logger.error(f"Error capturing home positions: {e}")
            self._home_ha_position = None
            self._home_dec_position = None

    def _calculate_safe_position_from_home(self) -> Tuple[float, float]:
        """Calculate a safe position that requires minimal movement from the current home position."""
        try:
            # Convert current encoder positions to HA/Dec
            ha_enc = self.status.ra_axis.encoder_position
            dec_enc = self.status.dec_axis.encoder_position
            
            if ha_enc is not None and dec_enc is not None:
                # Convert current encoder positions to HA/Dec coordinates
                current_ha, current_dec, below_pole = self.coordinates.encoder_positions_to_ha_dec(ha_enc, dec_enc)
                
                logger.info(f"Current position after homing: HA={current_ha:.3f}h, Dec={current_dec:.1f}°, below_pole={below_pole}")
                
                # Choose a safe position that's close to current but slightly adjusted
                # Move just a small amount to ensure we're not at the exact limit
                safe_ha = 0.0  # Move to meridian for HA
                safe_dec = current_dec + 2.0  # Move Dec by just 2 degrees away from home limit
                
                # Ensure the safe position is reachable
                if self.coordinates.is_position_reachable(safe_ha, safe_dec):
                    logger.info(f"Calculated safe position: HA={safe_ha:.3f}h, Dec={safe_dec:.1f}° (minimal movement required)")
                    return safe_ha, safe_dec
                else:
                    # If that's not reachable, try the other direction
                    safe_dec = current_dec - 2.0
                    if self.coordinates.is_position_reachable(safe_ha, safe_dec):
                        logger.info(f"Calculated safe position: HA={safe_ha:.3f}h, Dec={safe_dec:.1f}° (alternative minimal movement)")
                        return safe_ha, safe_dec
                    else:
                        # Fall back to staying very close to current position
                        safe_ha = current_ha + 0.1  # Move just 6 arcminutes in HA
                        safe_dec = current_dec + 0.5  # Move just 30 arcminutes in Dec
                        logger.warning(f"Using minimal adjustment from current position: HA={safe_ha:.3f}h, Dec={safe_dec:.1f}°")
                        return safe_ha, safe_dec
            else:
                logger.warning("Cannot determine current position for safe position calculation")
                return self.safe_position['ha'], self.safe_position['dec']
                
        except Exception as e:
            logger.error(f"Error calculating safe position from home: {e}")
            return self.safe_position['ha'], self.safe_position['dec']

    def start_tracking(self, mode: TrackingMode = TrackingMode.SIDEREAL) -> bool:
        """Start tracking at sidereal rate"""
        logger.info(f"Starting {mode.value} tracking")

        try:
            with self._command_lock:
                if mode == TrackingMode.SIDEREAL:
                    # Set sidereal tracking rate (15.04 arcsec/sec)
                    sidereal_rate = int(15.04 * self.calibration.ra_steps_per_degree / 3600)  # steps/sec

                    # Stop both axes first with timeouts
                    logger.debug("Stopping both axes before tracking with timeouts")
                    self.serial.send_command("$StopRA", "@StopRA", timeout=3.0)
                    self.serial.send_command("$StopDec", "@StopDec", timeout=3.0)

                    # Wait for stop to complete
                    time.sleep(0.5)

                    # Determine tracking direction based on pier side (pole position)
                    current_ha = self.status.current_hour_angle
                    if current_ha is not None:
                        below_pole = abs(current_ha) > 6.0

                        if below_pole:
                            # Under pole: track west to east (positive velocity)
                            tracking_velocity = sidereal_rate
                            # Set encoder position to a far positive position to allow continuous tracking
                            far_position = self.calibration.limits['ha_positive'] - 10000
                            logger.info(f"Below pole tracking: velocity={tracking_velocity}, target_pos={far_position}")
                        else:
                            # Normal pointing: track east to west (negative velocity)
                            tracking_velocity = -sidereal_rate
                            # Set encoder position to a far negative position to allow continuous tracking
                            far_position = self.calibration.limits['ha_negative'] + 10000
                            logger.info(f"Normal tracking: velocity={tracking_velocity}, target_pos={far_position}")

                        # Set the velocity for tracking
                        self.serial.send_command(f"$VelRA {tracking_velocity}", "@VelRA", timeout=3.0)

                        # Set the target position to the far position to allow continuous tracking
                        self.serial.send_command(f"$PosRA {far_position}", "@PosRA", timeout=3.0)

                        # Start tracking with timeout
                        logger.debug("Starting tracking motion")
                        self.serial.send_command("$RunRA", "@RunRA", timeout=3.0)

                        self.status.tracking_mode = TrackingMode.SIDEREAL
                        self.status.set_state(MountState.TRACKING)

                        logger.info(f"Sidereal tracking started: rate={tracking_velocity} steps/sec, below_pole={below_pole}")
                        return True
                    else:
                        logger.error("Cannot start tracking: current hour angle not available")
                        return False
                else:
                    logger.warning(f"Tracking mode {mode.value} not implemented")
                    return False

        except Exception as e:
            logger.error(f"Error starting tracking: {e}")
            return False

    def stop_tracking(self) -> bool:
        """Stop tracking"""
        logger.info("Stopping tracking")

        try:
            with self._command_lock:
                # Stop RA axis (tracking axis) with timeout
                self.serial.send_command("$StopRA", "@StopRA", timeout=3.0)

                self.status.tracking_mode = TrackingMode.STOPPED
                self.status.set_state(MountState.IDLE)

                logger.info("Tracking stopped")
                return True

        except Exception as e:
            logger.error(f"Error stopping tracking: {e}")
            return False

    def park_to_ha_dec(self, ha: float, dec: float, mode: SlewMode = SlewMode.NORMAL) -> bool:
        """Park telescope to specified HA/Dec coordinates - slew completion will set PARKED state"""
        logger.info(f"Parking to HA={ha:.3f}h, Dec={dec:.1f}°")

        try:
            with self._command_lock:
                # Enable parking mode so slew completion goes to PARKED
                self._parking_mode = True
                logger.debug(f"Parking mode enabled: parking_mode={self._parking_mode}")

                # Set state to parking
                self.status.set_state(MountState.PARKING)
                logger.debug(f"State set to PARKING: current_state={self.status.state}")

                # Stop tracking first
                self.stop_tracking()

                # Use the regular slew method which will go to PARKED when complete
                logger.debug(f"Starting park slew with parking_mode={self._parking_mode}")
                return self.slew_to_ha_dec(ha, dec, mode)

        except Exception as e:
            logger.error(f"Error during park: {e}")
            self._parking_mode = False  # Reset on error
            self.status.set_state(MountState.ERROR)
            return False

    def get_recent_faults(self) -> Optional[str]:
        """Get recent fault history"""
        try:
            response = self.serial.send_command("$RecentFaults", "@RecentFaults", timeout=5.0)
            return response if response else None
        except Exception as e:
            logger.error(f"Error getting recent faults: {e}")
            return None

    def get_encoder_positions(self) -> Tuple[Optional[int], Optional[int]]:
        """Get current encoder positions for both axes.

        Returns:
            Tuple of (ha_encoder_position, dec_encoder_position) in encoder steps.
            Returns None for any axis where position is unavailable.
        """
        ha_pos = self.status.ra_axis.encoder_position
        dec_pos = self.status.dec_axis.encoder_position

        logger.debug(f"Current encoder positions: HA={ha_pos}, Dec={dec_pos}")
        return ha_pos, dec_pos

    def get_home_encoder_positions(self) -> Tuple[Optional[int], Optional[int]]:
        """Get the encoder positions that were captured immediately after homing.

        Returns:
            Tuple of (ha_home_position, dec_home_position) in encoder steps.
            Returns None if homing positions were not captured.
        """
        ha_home = getattr(self, '_home_ha_position', None)
        dec_home = getattr(self, '_home_dec_position', None)

        logger.debug(f"Captured home encoder positions: HA={ha_home}, Dec={dec_home}")
        return ha_home, dec_home

    def update_calibration_limits(self, new_limits: Dict[str, int]) -> bool:
        """Update the telescope's calibration limits dynamically.

        Args:
            new_limits: Dictionary containing new limit values

        Returns:
            True if limits were updated successfully
        """
        try:
            # Update the calibration data limits
            self.calibration.limits.update(new_limits)

            # Update the coordinates system with new limits
            self.coordinates = Coordinates(self.status, self.calibration.__dict__)

            logger.info(f"Calibration limits updated: {new_limits}")
            return True

        except Exception as e:
            logger.error(f"Error updating calibration limits: {e}")
            return False

    def add_position_callback(self, callback: Callable[[float, float], None]) -> None:
        """Add callback for position updates"""
        self._position_callbacks.append(callback)

    def remove_position_callback(self, callback: Callable[[float, float], None]) -> None:
        """Remove position callback"""
        if callback in self._position_callbacks:
            self._position_callbacks.remove(callback)

    def add_error_callback(self, callback: Callable[[Exception], None]) -> None:
        """Add callback for error notifications"""
        self._error_callbacks.append(callback)

    def remove_error_callback(self, callback: Callable[[Exception], None]) -> None:
        """Remove error callback"""
        if callback in self._error_callbacks:
            self._error_callbacks.remove(callback)

    @property
    def current_position(self) -> Tuple[Optional[float], Optional[float]]:
        """Get current HA/Dec position"""
        return self.status.current_hour_angle, self.status.current_declination

    @property
    def target_position(self) -> Tuple[Optional[float], Optional[float]]:
        """Get target HA/Dec position"""
        return self.status.target_hour_angle, self.status.target_declination

    @property
    def is_moving(self) -> bool:
        """Check if telescope is moving"""
        return self.status.is_moving

    @property
    def is_tracking(self) -> bool:
        """Check if telescope is tracking"""
        return self.status.state == MountState.TRACKING

    def get_status_dict(self) -> Dict[str, Any]:
        """Get comprehensive status dictionary"""
        return {
            **self.status.to_dict(),
            'serial_stats': self.serial.get_stats(),
            'calibration': self.calibration.__dict__,
            'monitoring': {
                'active': self._monitoring_active,
                'interval': self.monitoring_interval
            }
        }

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()