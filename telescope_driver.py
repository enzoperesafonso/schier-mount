"""
Robust telescope driver for ROTSE-III fork-mounted equatorial mount.
Thread-safe driver with comprehensive safety features and error handling.
"""

import threading
import time
import logging
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, Tuple, Callable, Union
from dataclasses import dataclass

# Import modules
from communication import TelescopeCommunication
from coordinates import Coordinates
from state import MountStatus, MountState, TrackingMode, PierSide
from config import TelescopeConfig

logger = logging.getLogger(__name__)


class SlewMode(Enum):
    """Slewing speed modes"""
    PRECISE = "precise"     # Slow, accurate positioning  
    NORMAL = "normal"       # Standard slewing speed
    FAST = "fast"          # High-speed slewing


@dataclass
class InitializationResult:
    """Result of telescope initialization"""
    success: bool
    message: str
    home_ha_encoder: Optional[int] = None
    home_dec_encoder: Optional[int] = None
    duration_seconds: Optional[float] = None


class TelescopeDriver:
    """
    Main telescope driver for ROTSE-III mount.
    
    Key Features:
    - Thread-safe operation with comprehensive locking
    - Serial safety mechanisms to prevent controller freezing
    - Automatic initialization with limit calibration
    - Configurable slewing speeds and precision
    - Sidereal tracking with automatic limit detection
    - Comprehensive safety monitoring and limit checking
    - Robust error handling and recovery
    - Event callbacks for position and state updates
    
    CRITICAL SAFETY FEATURE:
    Always stops telescope axes before sending motion commands to prevent
    the serial controller from freezing, which is a known issue with ROTSE-III.
    """
    
    def __init__(self, config_file: Optional[Union[str, Path]] = None, 
                 port: Optional[str] = None, baudrate: Optional[int] = None):
        """
        Initialize telescope driver.
        
        Args:
            config_file: Path to configuration file
            port: Serial port override  
            baudrate: Baudrate override
        """
        # Load configuration
        self.config = TelescopeConfig(config_file)
        
        # Override serial settings if provided
        if port:
            self.config.set('serial.port', port)
        if baudrate:
            self.config.set('serial.baudrate', baudrate)
        
        # Initialize core components
        serial_config = self.config.get_serial_config()
        self.comm = TelescopeCommunication(
            serial_config['port'],
            serial_config['baudrate'], 
            serial_config['timeout']
        )
        
        self.status = MountStatus()
        self.coords: Optional[Coordinates] = None  # Created after connection
        
        # Thread management
        self._command_lock = threading.RLock()  # Reentrant lock for nested calls
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitoring_active = False
        self._shutdown_event = threading.Event()
        
        # State tracking
        self._initialized = False
        self._home_positions_captured = False
        
        # Motion tracking  
        self._commanded_ha_target: Optional[int] = None
        self._commanded_dec_target: Optional[int] = None
        self._slew_start_time: Optional[float] = None
        self._current_slew_mode = SlewMode.NORMAL
        
        # Parking state
        self._parking_mode = False
        
        # Tracking state
        self._tracking_direction: Optional[str] = None  # 'positive' or 'negative'
        
        # Event callbacks
        self._position_callbacks: list[Callable[[float, float], None]] = []
        self._state_callbacks: list[Callable[[MountState, MountState], None]] = []
        
        logger.info(f"TelescopeDriver initialized for {self.config.get('serial.port')}")
    
    def connect(self) -> bool:
        """
        Connect to telescope and initialize communication.
        
        Returns:
            True if connection successful
        """
        logger.info("Connecting to telescope")
        
        try:
            # Connect communication interface
            if not self.comm.connect():
                logger.error("Failed to connect to telescope")
                return False
            
            # Test communication
            if not self.comm.test_communication():
                logger.error("Communication test failed")
                self.comm.disconnect()
                return False
            
            # Initialize coordinates system with configuration
            calibration_data = self.config.get_calibration_data()
            self.coords = Coordinates(self.status, calibration_data)
            
            # Start status monitoring
            self._start_monitoring()
            
            # Update initial status
            self._update_telescope_status()
            
            # Set initial state
            self.status.set_state(MountState.IDLE)
            
            logger.info("Telescope connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.status.set_state(MountState.ERROR)
            return False
    
    def disconnect(self) -> None:
        """Disconnect from telescope and cleanup resources"""
        logger.info("Disconnecting telescope")
        
        try:
            # Stop telescope motion first
            self.stop()
            
            # Stop monitoring
            self._stop_monitoring()
            
            # Disconnect communication
            self.comm.disconnect()
            
            # Update state
            self.status.set_state(MountState.DISCONNECTED)
            
            logger.info("Telescope disconnected successfully")
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
    
    def initialize(self, move_to_safe_position: bool = True) -> InitializationResult:
        """
        Initialize telescope by homing and calibrating limits.
        
        This process:
        1. Homes telescope to HA positive and Dec negative limits
        2. Captures actual encoder positions at limits
        3. Updates coordinate system with precise limit values
        4. Optionally moves to a safe operating position
        
        Args:
            move_to_safe_position: Move away from limits after homing
            
        Returns:
            InitializationResult with success status and details
        """
        if not self.comm.is_connected():
            return InitializationResult(False, "Not connected to telescope")
        
        if not self.coords:
            return InitializationResult(False, "Coordinate system not initialized")
        
        logger.info("Starting telescope initialization")
        start_time = time.time()
        
        try:
            with self._command_lock:
                self.status.set_state(MountState.INITIALIZING)
                
                # Step 1: Home telescope to limits
                logger.info("Step 1: Homing telescope to encoder limits")
                if not self._execute_homing():
                    return InitializationResult(False, "Homing sequence failed")
                
                # Step 2: Capture home positions for limit calibration
                logger.info("Step 2: Capturing encoder positions at limits")
                time.sleep(3.0)  # Allow telescope to settle at limits
                self._update_telescope_status()
                
                home_ha = self.status.ra_axis.encoder_position
                home_dec = self.status.dec_axis.encoder_position
                
                if home_ha is None or home_dec is None:
                    logger.warning("Could not read encoder positions after homing")
                    home_ha, home_dec = None, None
                else:
                    # Update coordinate system and configuration with actual positions
                    self.coords.update_limits_from_initialization(home_ha, home_dec)
                    self.config.update_limits(home_ha, home_dec)
                    self._home_positions_captured = True
                    
                    logger.info(f"Captured home positions: HA={home_ha}, Dec={home_dec}")
                
                # Step 3: Move to safe position if requested
                if move_to_safe_position:
                    logger.info("Step 3: Moving to safe operating position")
                    safe_ha, safe_dec = self._calculate_safe_position()
                    
                    if not self._slew_to_coordinates_initialization(safe_ha, safe_dec):
                        logger.warning("Failed to reach safe position, but initialization succeeded")
                    else:
                        logger.info("Moved to safe position successfully")
                
                # Mark as initialized
                self._initialized = True
                self.status.set_state(MountState.IDLE)
                
                duration = time.time() - start_time
                logger.info(f"Telescope initialization completed in {duration:.1f} seconds")
                
                return InitializationResult(
                    success=True,
                    message="Initialization completed successfully", 
                    home_ha_encoder=home_ha,
                    home_dec_encoder=home_dec,
                    duration_seconds=duration
                )
                
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            self.status.set_state(MountState.ERROR)
            return InitializationResult(False, f"Initialization failed: {e}")
    
    def _execute_homing(self) -> bool:
        """Execute telescope homing sequence"""
        try:
            self.status.set_state(MountState.HOMING)
            
            # CRITICAL: Stop axes before homing to prevent serial freeze
            logger.debug("Stopping axes before homing")
            self.comm.send_command("$StopRA", "@StopRA")
            self.comm.send_command("$StopDec", "@StopDec")
            time.sleep(1.0)
            
            # Execute homing commands
            logger.info("Executing homing sequence")
            safety_config = self.config.get_safety_config()
            timeout = safety_config.get('slew_timeout_seconds', 300.0)
            
            ra_result = self.comm.send_command("$HomeRA", "@HomeRA")
            dec_result = self.comm.send_command("$HomeDec", "@HomeDec")
            
            if ra_result is None or dec_result is None:
                logger.error("Homing commands failed")
                return False
            
            # Wait for homing to complete
            logger.info(f"Waiting for homing completion (timeout: {timeout}s)")
            time.sleep(min(120.0, timeout))  # Wait up to 2 minutes or configured timeout
            
            return True
            
        except Exception as e:
            logger.error(f"Homing execution failed: {e}")
            return False
    
    def _calculate_safe_position(self) -> Tuple[float, float]:
        """Calculate safe position away from limits"""
        try:
            # Get current position if available
            current_ha, current_dec = self.get_position()
            
            if current_ha is not None and current_dec is not None:
                # Move to meridian (HA=0) and slightly positive declination
                safe_ha = 0.0  # Hour angle = 0 (on meridian)
                safe_dec = -90
                
                logger.info(f"Calculated safe position: HA={safe_ha:.3f}h, Dec={safe_dec:.1f}°")
                return safe_ha, safe_dec
            else:
                # Fallback safe position
                logger.warning("Using fallback safe position")
                return 0.0, -90.0  # Meridian, 15° south
                
        except Exception as e:
            logger.error(f"Error calculating safe position: {e}")
            return 0.0, -15.0
    
    def slew_to_coordinates(self, ha: float, dec: float, mode: SlewMode = SlewMode.NORMAL) -> bool:
        """
        Slew telescope to specified HA/Dec coordinates.
        
        Args:
            ha: Hour angle in hours (-12 to +12)
            dec: Declination in degrees (-90 to +90)
            mode: Slewing speed mode
            
        Returns:
            True if slew started successfully
        """
        if not self._initialized:
            logger.error("Telescope not initialized")
            return False
        
        if not self.coords:
            logger.error("Coordinate system not available")
            return False
        
        logger.info(f"Slewing to HA={ha:.4f}h, Dec={dec:.3f}° in {mode.value} mode")
        
        try:
            with self._command_lock:
                # Check if position is reachable
                if not self.coords.is_position_reachable(ha, dec):
                    logger.error(f"Position not reachable: HA={ha:.4f}h, Dec={dec:.3f}°")
                    return False
                
                # Check safety limits
                if not self._check_slew_safety(ha, dec):
                    logger.error("Slew target fails safety checks")
                    return False
                
                # Set motion parameters for selected mode
                motion_params = self.config.get_motion_params(mode.value)
                self._set_motion_parameters(motion_params)
                
                # Execute slew
                success = self._slew_to_coordinates_internal(ha, dec)
                
                if success:
                    self._current_slew_mode = mode
                    if not self._parking_mode:  # Only set SLEWING if not parking
                        self.status.set_state(MountState.SLEWING)
                    
                    self._slew_start_time = time.time()
                    logger.info(f"Slew started successfully in {mode.value} mode")
                else:
                    logger.error("Failed to start slew")
                
                return success
                
        except Exception as e:
            logger.error(f"Slew failed: {e}")
            self.status.set_state(MountState.ERROR)
            return False
    
    def _slew_to_coordinates_initialization(self, ha: float, dec: float) -> bool:
        """Special slew method for initialization with conservative parameters"""
        try:
            with self._command_lock:
                # Use initialization motion parameters (conservative)
                motion_params = self.config.get_motion_params('initialization')
                self._set_motion_parameters(motion_params)
                
                # Execute slew
                success = self._slew_to_coordinates_internal(ha, dec)
                
                if success:
                    self.status.set_state(MountState.SLEWING)
                    self._slew_start_time = time.time()
                    
                    # Wait for completion with tighter tolerance
                    safety_config = self.config.get_safety_config()
                    timeout = safety_config.get('slew_timeout_seconds', 300.0)
                    tolerance = safety_config.get('initialization_tolerance_steps', 2000)
                    
                    return self._wait_for_slew_completion(timeout, tolerance)
                
                return False
                
        except Exception as e:
            logger.error(f"Initialization slew failed: {e}")
            return False
    
    def _slew_to_coordinates_internal(self, ha: float, dec: float) -> bool:
        """Internal coordinate slew implementation"""
        try:
            # Convert to encoder positions
            ha_enc, dec_enc, below_pole = self.coords.ha_dec_to_encoder_positions(ha, dec)
            
            # Set target coordinates for status tracking
            self.status.set_target_coordinates(ha, dec)
            
            # Execute encoder slew
            return self._slew_to_encoders(ha_enc, dec_enc)
            
        except Exception as e:
            logger.error(f"Internal slew failed: {e}")
            return False
    
    def _slew_to_encoders(self, ha_enc: int, dec_enc: int) -> bool:
        """Execute low-level slew to encoder positions"""
        try:
            # CRITICAL SAFETY: Always stop axes before motion commands
            # This prevents the ROTSE-III controller from freezing
            logger.debug("Stopping axes before slew commands (CRITICAL for ROTSE-III)")
            self.comm.send_command("$StopRA", "@StopRA")
            self.comm.send_command("$StopDec", "@StopDec")
            time.sleep(0.5)  # Allow stop commands to take effect
            
            # Store commanded targets for completion checking
            self._commanded_ha_target = ha_enc
            self._commanded_dec_target = dec_enc
            
            # Send position commands
            logger.debug(f"Setting target positions: RA={ha_enc}, Dec={dec_enc}")
            ra_result = self.comm.send_command(f"$PosRA {ha_enc}", "@PosRA")
            dec_result = self.comm.send_command(f"$PosDec {dec_enc}", "@PosDec")
            
            if ra_result is None or dec_result is None:
                logger.error("Failed to set target positions")
                return False
            
            # Start motion
            logger.debug("Starting motion on both axes")
            ra_run = self.comm.send_command("$RunRA", "@RunRA")
            dec_run = self.comm.send_command("$RunDec", "@RunDec")
            
            if ra_run is None or dec_run is None:
                logger.error("Failed to start motion")
                return False
            
            logger.info(f"Slew commands executed: RA={ha_enc}, Dec={dec_enc}")
            return True
            
        except Exception as e:
            logger.error(f"Encoder slew failed: {e}")
            return False
    
    def _set_motion_parameters(self, params: Dict[str, int]) -> None:
        """Set telescope motion parameters"""
        try:
            # Set velocities
            self.comm.send_command(f"$VelRA {params['ha_velocity']}", "@VelRA")
            self.comm.send_command(f"$VelDec {params['dec_velocity']}", "@VelDec")
            
            # Set accelerations
            self.comm.send_command(f"$AccelRA {params['ha_acceleration']}", "@AccelRA")
            self.comm.send_command(f"$AccelDec {params['dec_acceleration']}", "@AccelDec")
            
            logger.debug(f"Motion parameters set: {params}")
            
        except Exception as e:
            logger.error(f"Failed to set motion parameters: {e}")
            raise
    
    def stop(self) -> bool:
        """Stop all telescope motion"""
        logger.info("Stopping telescope")
        
        try:
            with self._command_lock:
                self.status.set_state(MountState.STOPPING)
                
                # Send stop commands
                self.comm.send_command("$StopRA", "@StopRA")
                self.comm.send_command("$StopDec", "@StopDec")
                
                # Clear motion tracking
                self._commanded_ha_target = None
                self._commanded_dec_target = None
                self._parking_mode = False
                self._tracking_direction = None
                
                time.sleep(0.5)
                self.status.set_state(MountState.IDLE)
                
                logger.info("Telescope stopped successfully")
                return True
                
        except Exception as e:
            logger.error(f"Stop failed: {e}")
            return False
    
    def emergency_stop(self) -> bool:
        """Emergency stop with maximum priority"""
        logger.critical("EMERGENCY STOP activated")
        
        try:
            # Use communication emergency stop
            success = self.comm.emergency_stop()
            
            if success:
                # Clear all motion state
                self._commanded_ha_target = None
                self._commanded_dec_target = None
                self._parking_mode = False
                self._tracking_direction = None
                
                self.status.set_state(MountState.HALTED)
                logger.info("Emergency stop completed")
            else:
                logger.error("Emergency stop failed")
                self.status.set_state(MountState.ERROR)
            
            return success
            
        except Exception as e:
            logger.error(f"Emergency stop error: {e}")
            self.status.set_state(MountState.ERROR)
            return False
    
    def start_tracking(self, mode: TrackingMode = TrackingMode.SIDEREAL) -> bool:
        """
        Start sidereal tracking.
        
        Args:
            mode: Tracking mode (currently only SIDEREAL supported)
            
        Returns:
            True if tracking started successfully
        """
        if not self._initialized:
            logger.error("Telescope not initialized")
            return False
        
        if mode != TrackingMode.SIDEREAL:
            logger.error(f"Tracking mode {mode.value} not implemented")
            return False
        
        logger.info("Starting sidereal tracking")
        
        try:
            with self._command_lock:
                # Get current position to determine tracking direction
                current_ha, current_dec = self.get_position()
                
                if current_ha is None:
                    logger.error("Cannot start tracking: position unknown")
                    return False
                
                # Determine tracking direction and parameters
                tracking_config = self.config.get_tracking_config()
                sidereal_rate = int(tracking_config['sidereal_rate_steps_per_sec'])
                
                # Check pier side to determine tracking direction
                below_pole = abs(current_ha) > 6.0
                
                if below_pole:
                    # Below pole: track west to east (positive direction)
                    tracking_velocity = sidereal_rate
                    self._tracking_direction = 'positive'
                    # Set target far from positive limit
                    safe_limits = self.coords.get_safe_limits(tracking_config['tracking_safety_margin_steps'])
                    target_position = safe_limits['ha_positive'] - 5000
                else:
                    # Normal: track east to west (negative direction) 
                    tracking_velocity = -sidereal_rate
                    self._tracking_direction = 'negative'
                    # Set target far from negative limit
                    safe_limits = self.coords.get_safe_limits(tracking_config['tracking_safety_margin_steps'])
                    target_position = safe_limits['ha_negative'] + 5000
                
                # CRITICAL: Stop axes before tracking commands
                self.comm.send_command("$StopRA", "@StopRA")
                self.comm.send_command("$StopDec", "@StopDec")
                time.sleep(0.5)
                
                # Set tracking parameters
                self.comm.send_command(f"$VelRA {tracking_velocity}", "@VelRA")
                self.comm.send_command(f"$PosRA {target_position}", "@PosRA")
                
                # Start tracking motion
                result = self.comm.send_command("$RunRA", "@RunRA")
                
                if result is None:
                    logger.error("Failed to start tracking motion")
                    return False
                
                # Update state
                self.status.tracking_mode = TrackingMode.SIDEREAL
                self.status.set_state(MountState.TRACKING)
                
                logger.info(f"Sidereal tracking started: rate={tracking_velocity} steps/sec, direction={self._tracking_direction}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to start tracking: {e}")
            return False
    
    def stop_tracking(self) -> bool:
        """Stop sidereal tracking"""
        logger.info("Stopping tracking")
        
        try:
            with self._command_lock:
                # Stop RA axis (tracking axis)
                self.comm.send_command("$StopRA", "@StopRA")
                
                # Clear tracking state
                self.status.tracking_mode = TrackingMode.STOPPED
                self._tracking_direction = None
                self.status.set_state(MountState.IDLE)
                
                logger.info("Tracking stopped successfully")
                return True
                
        except Exception as e:
            logger.error(f"Failed to stop tracking: {e}")
            return False
    
    def park(self, ha: float = 0.0, dec: float = -20.0) -> bool:
        """
        Park telescope at specified coordinates.
        
        Args:
            ha: Park hour angle in hours
            dec: Park declination in degrees
            
        Returns:
            True if parking started successfully
        """
        logger.info(f"Parking telescope at HA={ha:.3f}h, Dec={dec:.1f}°")
        
        try:
            with self._command_lock:
                # Stop tracking if active
                if self.status.state == MountState.TRACKING:
                    self.stop_tracking()
                
                # Enable parking mode
                self._parking_mode = True
                self.status.set_state(MountState.PARKING)
                
                # Execute slew to park position
                success = self.slew_to_coordinates(ha, dec, SlewMode.NORMAL)
                
                if not success:
                    self._parking_mode = False
                    logger.error("Failed to start parking slew")
                    return False
                
                logger.info("Parking sequence started")
                return True
                
        except Exception as e:
            logger.error(f"Parking failed: {e}")
            self._parking_mode = False
            return False
    
    def unpark(self) -> bool:
        """
        Unpark telescope (simply changes state from PARKED to IDLE).
        
        Returns:
            True if successful
        """
        if self.status.state != MountState.PARKED:
            logger.warning(f"Cannot unpark: telescope is in {self.status.state.value} state")
            return False
        
        logger.info("Unparking telescope")
        self.status.set_state(MountState.IDLE)
        logger.info("Telescope unparked")
        return True
    
    def get_position(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Get current telescope position.
        
        Returns:
            Tuple of (hour_angle, declination) or (None, None) if unavailable
        """
        return self.status.current_hour_angle, self.status.current_declination
    
    def get_encoder_positions(self) -> Tuple[Optional[int], Optional[int]]:
        """
        Get current encoder positions.
        
        Returns:
            Tuple of (ha_encoder, dec_encoder) or (None, None) if unavailable
        """
        return (self.status.ra_axis.encoder_position, 
                self.status.dec_axis.encoder_position)
    
    def is_initialized(self) -> bool:
        """Check if telescope is initialized"""
        return self._initialized
    
    def is_connected(self) -> bool:
        """Check if telescope is connected"""
        return self.comm.is_connected()
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive telescope status"""
        return {
            'state': self.status.state.value,
            'tracking_mode': self.status.tracking_mode.value,
            'initialized': self._initialized,
            'connected': self.is_connected(),
            'position': {
                'ha': self.status.current_hour_angle,
                'dec': self.status.current_declination,
                'pier_side': self.status.pier_side.value
            },
            'encoders': {
                'ha': self.status.ra_axis.encoder_position,
                'dec': self.status.dec_axis.encoder_position
            },
            'communication': self.comm.get_statistics(),
            'configuration': {
                'serial_port': self.config.get('serial.port'),
                'observer_latitude': self.config.get('coordinates.observer_latitude')
            }
        }
    
    def _check_slew_safety(self, ha: float, dec: float) -> bool:
        """Check if slew target is safe"""
        try:
            if not self.coords:
                return False
            
            # Check basic reachability
            if not self.coords.is_position_reachable(ha, dec):
                logger.error(f"Target position not reachable: HA={ha:.4f}h, Dec={dec:.3f}°")
                return False
            
            # Convert to encoder positions
            ha_enc, dec_enc, below_pole = self.coords.ha_dec_to_encoder_positions(ha, dec)
            
            # Check safety limits
            safety_config = self.config.get_safety_config()
            safety_margin = safety_config.get('safety_margin_steps', 20000)
            safe_limits = self.coords.get_safe_limits(safety_margin)
            
            if not (safe_limits['ha_negative'] <= ha_enc <= safe_limits['ha_positive']):
                logger.error(f"HA encoder {ha_enc} outside safe limits [{safe_limits['ha_negative']}, {safe_limits['ha_positive']}]")
                return False
            
            if not (safe_limits['dec_negative'] <= dec_enc <= safe_limits['dec_positive']):
                logger.error(f"Dec encoder {dec_enc} outside safe limits [{safe_limits['dec_negative']}, {safe_limits['dec_positive']}]")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Safety check failed: {e}")
            return False
    
    def _start_monitoring(self) -> None:
        """Start status monitoring thread"""
        if self._monitoring_active:
            return
        
        self._monitoring_active = True
        self._shutdown_event.clear()
        
        self._monitor_thread = threading.Thread(
            target=self._monitoring_loop,
            name="telescope_monitor",
            daemon=True
        )
        self._monitor_thread.start()
        
        logger.info("Status monitoring started")
    
    def _stop_monitoring(self) -> None:
        """Stop monitoring thread"""
        self._monitoring_active = False
        self._shutdown_event.set()
        
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)
    
    def _monitoring_loop(self) -> None:
        """Main monitoring loop"""
        logger.info("Telescope monitoring loop started")
        
        monitoring_config = self.config.get_monitoring_config()
        update_interval = monitoring_config.get('status_update_interval', 1.0)
        
        while self._monitoring_active and not self._shutdown_event.is_set():
            try:
                # Update telescope status
                self._update_telescope_status()
                
                # Check for slew completion
                if self.status.state in [MountState.SLEWING, MountState.PARKING]:
                    self._check_slew_completion()
                
                # Check tracking safety
                if self.status.state == MountState.TRACKING:
                    self._check_tracking_safety()
                
                # Notify callbacks
                self._notify_position_callbacks()
                
                # Sleep until next update
                if self._shutdown_event.wait(update_interval):
                    break
                    
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(update_interval)
        
        logger.info("Telescope monitoring loop stopped")
    
    def _update_telescope_status(self) -> None:
        """Update telescope status from hardware"""
        try:
            # Get Status1 (positions)
            ra_status1 = self.comm.send_command("$Status1RA", "@Status1RA")
            dec_status1 = self.comm.send_command("$Status1Dec", "@Status1Dec")
            
            # Parse positions
            if ra_status1 and "," in ra_status1:
                parts = ra_status1.split(",")
                if len(parts) >= 2:
                    cmd_pos = int(float(parts[0].strip()))
                    actual_pos = int(float(parts[1].strip()))
                    self.status.update_axis_from_status1("RA", cmd_pos, actual_pos)
            
            if dec_status1 and "," in dec_status1:
                parts = dec_status1.split(",")
                if len(parts) >= 2:
                    cmd_pos = int(float(parts[0].strip()))
                    actual_pos = int(float(parts[1].strip()))
                    self.status.update_axis_from_status1("Dec", cmd_pos, actual_pos)
            
            # Get Status2 (status words)
            ra_status2 = self.comm.send_command("$Status2RA", "@Status2RA")
            dec_status2 = self.comm.send_command("$Status2Dec", "@Status2Dec")
            
            if ra_status2:
                self.status.update_axis_from_status2("RA", ra_status2)
            if dec_status2:
                self.status.update_axis_from_status2("Dec", dec_status2)
            
            # Update coordinates if we have valid positions and coordinate system
            ha_pos = self.status.ra_axis.encoder_position
            dec_pos = self.status.dec_axis.encoder_position
            
            if ha_pos is not None and dec_pos is not None and self.coords:
                try:
                    ha, dec, below_pole = self.coords.encoder_positions_to_ha_dec(ha_pos, dec_pos)
                    pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
                    self.status.set_coordinates(ha, dec, pier_side)
                except Exception as e:
                    logger.debug(f"Coordinate conversion error: {e}")
            
        except Exception as e:
            logger.error(f"Status update failed: {e}")
    
    def _check_slew_completion(self) -> None:
        """Check if slew has completed"""
        if (self._commanded_ha_target is None or 
            self._commanded_dec_target is None):
            return
        
        # Check if both axes are at target
        ha_at_target = self._is_axis_at_target("RA")
        dec_at_target = self._is_axis_at_target("Dec")
        
        if ha_at_target and dec_at_target:
            logger.info("Slew completed - both axes at target")
            
            # Send final stop commands
            try:
                self.comm.send_command("$StopRA", "@StopRA")
                self.comm.send_command("$StopDec", "@StopDec")
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Error sending final stop commands: {e}")
            
            # Update state based on mode
            if self._parking_mode:
                logger.info("Parking completed")
                self.status.set_state(MountState.PARKED)
                self._parking_mode = False
            else:
                self.status.set_state(MountState.IDLE)
            
            # Clear targets
            self._commanded_ha_target = None
            self._commanded_dec_target = None
            self._slew_start_time = None
        
        # Check for timeout
        elif (self._slew_start_time and 
              time.time() - self._slew_start_time > self.config.get_safety_config().get('slew_timeout_seconds', 300.0)):
            logger.error("Slew timeout - executing emergency stop")
            self.emergency_stop()
    
    def _is_axis_at_target(self, axis: str) -> bool:
        """Check if axis is at commanded target"""
        axis_status = self.status.ra_axis if axis.upper() == "RA" else self.status.dec_axis
        target = self._commanded_ha_target if axis.upper() == "RA" else self._commanded_dec_target
        
        if axis_status.encoder_position is None or target is None:
            return False
        
        error = abs(axis_status.encoder_position - target)
        tolerance = self.config.get_safety_config().get('position_tolerance_steps', 5000)
        
        return error <= tolerance
    
    def _check_tracking_safety(self) -> None:
        """Monitor tracking safety and stop before limits"""
        if not self._tracking_direction or not self.coords:
            return
        
        ha_pos = self.status.ra_axis.encoder_position
        if ha_pos is None:
            return
        
        try:
            tracking_config = self.config.get_tracking_config()
            safety_margin = tracking_config.get('tracking_safety_margin_steps', 10000)
            
            # Calculate distance to limit
            distance = self.coords.get_tracking_limit_distance(ha_pos, self._tracking_direction)
            
            if distance <= safety_margin:
                logger.warning(f"Tracking approaching {self._tracking_direction} limit: {distance} steps remaining")
                logger.info("Stopping tracking for safety")
                self.stop_tracking()
                
        except Exception as e:
            logger.error(f"Tracking safety check failed: {e}")
    
    def _wait_for_slew_completion(self, timeout: float, tolerance: int) -> bool:
        """Wait for slew completion with custom parameters"""
        start_time = time.time()
        
        # Temporarily adjust tolerance
        safety_config = self.config.get_safety_config()
        original_tolerance = safety_config.get('position_tolerance_steps', 5000)
        safety_config['position_tolerance_steps'] = tolerance
        
        try:
            while time.time() - start_time < timeout:
                if self.status.state not in [MountState.SLEWING, MountState.PARKING]:
                    return self.status.state != MountState.ERROR
                
                time.sleep(0.5)
            
            logger.error(f"Slew did not complete within {timeout} seconds")
            return False
            
        finally:
            # Restore original tolerance
            safety_config['position_tolerance_steps'] = original_tolerance
    
    def _notify_position_callbacks(self) -> None:
        """Notify position update callbacks"""
        ha, dec = self.get_position()
        if ha is not None and dec is not None:
            for callback in self._position_callbacks.copy():
                try:
                    callback(ha, dec)
                except Exception as e:
                    logger.error(f"Error in position callback: {e}")
    
    def add_position_callback(self, callback: Callable[[float, float], None]) -> None:
        """Add position update callback"""
        if callback not in self._position_callbacks:
            self._position_callbacks.append(callback)
    
    def remove_position_callback(self, callback: Callable[[float, float], None]) -> None:
        """Remove position update callback"""
        if callback in self._position_callbacks:
            self._position_callbacks.remove(callback)
    
    def add_state_callback(self, callback: Callable[[MountState, MountState], None]) -> None:
        """Add state change callback"""
        self.status.add_state_callback(callback)
    
    def remove_state_callback(self, callback: Callable[[MountState, MountState], None]) -> None:
        """Remove state change callback"""
        self.status.remove_state_callback(callback)
    
    def __enter__(self):
        """Context manager entry"""
        if self.connect():
            return self
        else:
            raise RuntimeError("Failed to connect to telescope")
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()
    
    def __str__(self) -> str:
        """String representation"""
        return (f"TelescopeDriver(port={self.config.get('serial.port')}, "
                f"state={self.status.state.value}, initialized={self._initialized})")