"""
Async telescope driver for ROTSE-III fork-mounted equatorial mount.
Demonstrates the benefits of asyncio for telescope control.
"""

import asyncio
import logging
import time
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, Tuple, Callable, Union
from dataclasses import dataclass

# Import modules
from communication import AsyncTelescopeCommunication
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


class AsyncTelescopeDriver:
    """
    Async telescope driver for ROTSE-III mount.
    
    Key Benefits of Asyncio:
    - Single-threaded event loop eliminates race conditions
    - Clean async/await syntax for sequential operations
    - Built-in timeout handling with asyncio.wait_for()
    - Non-blocking concurrent operations (monitoring + commands)
    - Easy cancellation of long-running operations
    - Deterministic execution order for testing
    
    Features:
    - Async serial communication with automatic retry
    - Concurrent status monitoring without threads
    - Safe command sequencing with asyncio.Lock()
    - Automatic timeout handling for all operations
    - Graceful shutdown with proper resource cleanup
    """
    
    def __init__(self, config_file: Optional[Union[str, Path]] = None, 
                 port: Optional[str] = None, baudrate: Optional[int] = None):
        """
        Initialize async telescope driver.
        
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
        self.comm = AsyncTelescopeCommunication(
            serial_config['port'],
            serial_config['baudrate'], 
            serial_config['timeout']
        )
        
        self.status = MountStatus()
        self.coords: Optional[Coordinates] = None
        
        # Async coordination
        self._command_lock = asyncio.Lock()
        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # State tracking
        self._initialized = False
        self._commanded_ha_target: Optional[int] = None
        self._commanded_dec_target: Optional[int] = None
        self._slew_start_time: Optional[float] = None
        self._tracking_direction: Optional[str] = None
        self._parking_mode = False
        self._park_position: Optional[Tuple[float, float]] = None
        
        logger.info(f"AsyncTelescopeDriver initialized for {self.config.get('serial.port')}")
    
    async def connect(self) -> bool:
        """
        Connect to telescope and initialize communication.
        
        Returns:
            True if connection successful
        """
        logger.info("Connecting to telescope")
        
        try:
            # Connect communication interface
            if not await self.comm.connect():
                logger.error("Failed to connect to telescope")
                return False
            
            # Test communication
            if not await self.comm.test_communication():
                logger.error("Communication test failed")
                await self.comm.disconnect()
                return False
            
            # Initialize coordinates system with configuration
            calibration_data = self.config.get_calibration_data()
            self.coords = Coordinates(self.status, calibration_data)
            
            # Start status monitoring
            await self._start_monitoring()
            
            # Update initial status
            await self._update_telescope_status()
            
            # Set initial state to PARKED (telescope starts parked for safety)
            self.status.set_state(MountState.PARKED)
            
            logger.info("Telescope connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.status.set_state(MountState.ERROR)
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from telescope and cleanup resources"""
        logger.info("Disconnecting telescope")
        
        try:
            # Stop telescope motion first
            await self.stop()
            
            # Stop monitoring
            await self._stop_monitoring()
            
            # Disconnect communication
            await self.comm.disconnect()
            
            # Update state
            self.status.set_state(MountState.DISCONNECTED)
            
            logger.info("Telescope disconnected successfully")
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
    
    async def initialize(self, move_to_safe_position: bool = True) -> InitializationResult:
        """
        Initialize telescope by homing and calibrating limits.
        
        This async method demonstrates clean sequential operations:
        1. Homes telescope to limits
        2. Captures encoder positions
        3. Updates coordinate system
        4. Moves to safe position
        
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
            async with self._command_lock:
                self.status.set_state(MountState.INITIALIZING)
                
                # Step 1: Home telescope to limits
                logger.info("Step 1: Homing telescope to encoder limits")
                if not await self._execute_homing():
                    return InitializationResult(False, "Homing sequence failed")
                
                # Step 2: Capture home positions for limit calibration
                logger.info("Step 2: Capturing encoder positions at limits")
                await asyncio.sleep(3.0)  # Allow telescope to settle at limits
                await self._update_telescope_status()
                
                home_ha = self.status.ra_axis.encoder_position
                home_dec = self.status.dec_axis.encoder_position
                
                if home_ha is not None and home_dec is not None:
                    # Update coordinate system and configuration with actual positions
                    self.coords.update_limits_from_initialization(home_ha, home_dec)
                    self.config.update_limits(home_ha, home_dec)
                    
                    logger.info(f"Captured home positions: HA={home_ha}, Dec={home_dec}")
                
                # Step 3: Move to safe position if requested
                if move_to_safe_position:
                    logger.info("Step 3: Moving to safe operating position")
                    safe_ha, safe_dec = self._calculate_safe_position()
                    
                    if not await self._slew_to_coordinates_async(safe_ha, safe_dec):
                        logger.warning("Failed to reach safe position, but initialization succeeded")
                    else:
                        logger.info("Slew to safe position started, waiting for completion...")
                        
                        # Wait for slew completion while staying in INITIALIZING state
                        safety_config = self.config.get_safety_config()
                        timeout = safety_config.get('slew_timeout_seconds', 300.0)
                        tolerance = safety_config.get('initialization_tolerance_steps', 2000)
                        
                        if await self._wait_for_slew_completion_async(timeout, tolerance):
                            logger.info("Moved to safe position successfully")
                        else:
                            logger.warning("Safe position move timed out, but initialization succeeded")
                
                # Mark as initialized only after everything is complete
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
    
    async def _execute_homing(self) -> bool:
        """Execute telescope homing sequence"""
        try:
            self.status.set_state(MountState.HOMING)
            
            # CRITICAL: Stop axes before homing to prevent serial freeze
            logger.debug("Stopping axes before homing")
            await self.comm.send_command("$StopRA", "@StopRA")
            await self.comm.send_command("$StopDec", "@StopDec")
            await asyncio.sleep(1.0)
            
            # Execute homing commands
            logger.info("Executing homing sequence")
            safety_config = self.config.get_safety_config()
            timeout = safety_config.get('slew_timeout_seconds', 300.0)
            
            ra_result = await self.comm.send_command("$HomeRA", "@HomeRA")
            dec_result = await self.comm.send_command("$HomeDec", "@HomeDec")
            
            if ra_result is None or dec_result is None:
                logger.error("Homing commands failed")
                return False
            
            # Wait for homing to complete with timeout
            logger.info(f"Waiting for homing completion (timeout: {timeout}s)")
            await asyncio.sleep(min(120.0, timeout))  # Wait up to 2 minutes or configured timeout
            
            return True
            
        except Exception as e:
            logger.error(f"Homing execution failed: {e}")
            return False
    
    async def slew_to_coordinates(self, ha: float, dec: float, mode: SlewMode = SlewMode.NORMAL) -> bool:
        """
        Slew telescope to specified HA/Dec coordinates (async).
        
        Demonstrates async benefits:
        - Clean sequential execution with async/await
        - Built-in timeout handling
        - Easy cancellation if needed
        
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
            async with self._command_lock:
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
                await self._set_motion_parameters(motion_params)
                
                # Execute slew
                success = await self._slew_to_coordinates_async(ha, dec)
                
                if success:
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
    
    async def _slew_to_coordinates_async(self, ha: float, dec: float) -> bool:
        """Internal async coordinate slew implementation"""
        try:
            # Convert to encoder positions
            ha_enc, dec_enc, below_pole = self.coords.ha_dec_to_encoder_positions(ha, dec)
            
            # Set target coordinates for status tracking
            self.status.set_target_coordinates(ha, dec)
            
            # Execute encoder slew
            return await self._slew_to_encoders_async(ha_enc, dec_enc)
            
        except Exception as e:
            logger.error(f"Internal async slew failed: {e}")
            return False
    
    async def _slew_to_encoders_async(self, ha_enc: int, dec_enc: int) -> bool:
        """Execute low-level async slew to encoder positions"""
        try:
            # CRITICAL SAFETY: Always stop axes before motion commands
            logger.debug("Stopping axes before slew commands (CRITICAL for ROTSE-III)")
            await self.comm.send_command("$StopRA", "@StopRA")
            await self.comm.send_command("$StopDec", "@StopDec")
            await asyncio.sleep(0.5)  # Allow stop commands to take effect
            
            # Store commanded targets for completion checking
            self._commanded_ha_target = ha_enc
            self._commanded_dec_target = dec_enc
            
            # Send position commands
            logger.debug(f"Setting target positions: RA={ha_enc}, Dec={dec_enc}")
            ra_result = await self.comm.send_command(f"$PosRA {ha_enc}", "@PosRA")
            dec_result = await self.comm.send_command(f"$PosDec {dec_enc}", "@PosDec")
            
            if ra_result is None or dec_result is None:
                logger.error("Failed to set target positions")
                return False
            
            # Start motion
            logger.debug("Starting motion on both axes")
            ra_run = await self.comm.send_command("$RunRA", "@RunRA")
            dec_run = await self.comm.send_command("$RunDec", "@RunDec")
            
            if ra_run is None or dec_run is None:
                logger.error("Failed to start motion")
                return False
            
            logger.info(f"Slew commands executed: RA={ha_enc}, Dec={dec_enc}")
            return True
            
        except Exception as e:
            logger.error(f"Async encoder slew failed: {e}")
            return False
    
    async def _set_motion_parameters(self, params: Dict[str, int]) -> None:
        """Set telescope motion parameters (async)"""
        try:
            # Set velocities and accelerations concurrently
            await asyncio.gather(
                self.comm.send_command(f"$VelRA {params['ha_velocity']}", "@VelRA"),
                self.comm.send_command(f"$VelDec {params['dec_velocity']}", "@VelDec"),
                self.comm.send_command(f"$AccelRA {params['ha_acceleration']}", "@AccelRA"),
                self.comm.send_command(f"$AccelDec {params['dec_acceleration']}", "@AccelDec")
            )
            
            logger.debug(f"Motion parameters set: {params}")
            
        except Exception as e:
            logger.error(f"Failed to set motion parameters: {e}")
            raise
    
    async def stop(self) -> bool:
        """Stop all telescope motion (async)"""
        logger.info("Stopping telescope")
        
        try:
            async with self._command_lock:
                self.status.set_state(MountState.STOPPING)
                
                # Send stop commands concurrently
                await asyncio.gather(
                    self.comm.send_command("$StopRA", "@StopRA"),
                    self.comm.send_command("$StopDec", "@StopDec")
                )
                
                # Clear motion tracking
                self._commanded_ha_target = None
                self._commanded_dec_target = None
                self._tracking_direction = None
                self._parking_mode = False
                
                await asyncio.sleep(0.5)
                self.status.set_state(MountState.IDLE)
                
                logger.info("Telescope stopped successfully")
                return True
                
        except Exception as e:
            logger.error(f"Stop failed: {e}")
            return False
    
    async def start_tracking(self, mode: TrackingMode = TrackingMode.SIDEREAL) -> bool:
        """
        Start sidereal tracking (async).
        
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
            async with self._command_lock:
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
                    safe_limits = self.coords.get_safe_limits(tracking_config['tracking_safety_margin_steps'])
                    target_position = safe_limits['ha_positive'] - 5000
                else:
                    # Normal: track east to west (negative direction) 
                    tracking_velocity = -sidereal_rate
                    self._tracking_direction = 'negative'
                    safe_limits = self.coords.get_safe_limits(tracking_config['tracking_safety_margin_steps'])
                    target_position = safe_limits['ha_negative'] + 5000
                
                # CRITICAL: Stop axes before tracking commands
                await self.comm.send_command("$StopRA", "@StopRA")
                await self.comm.send_command("$StopDec", "@StopDec")
                await asyncio.sleep(0.5)
                
                # Set tracking parameters and start motion
                await asyncio.gather(
                    self.comm.send_command(f"$VelRA {tracking_velocity}", "@VelRA"),
                    self.comm.send_command(f"$PosRA {target_position}", "@PosRA")
                )
                
                result = await self.comm.send_command("$RunRA", "@RunRA")
                
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
    
    async def stop_tracking(self) -> bool:
        """Stop sidereal tracking (async)"""
        logger.info("Stopping tracking")
        
        try:
            async with self._command_lock:
                # Stop RA axis (tracking axis)
                await self.comm.send_command("$StopRA", "@StopRA")
                
                # Clear tracking state
                self.status.tracking_mode = TrackingMode.STOPPED
                self._tracking_direction = None
                self.status.set_state(MountState.IDLE)
                
                logger.info("Tracking stopped successfully")
                return True
                
        except Exception as e:
            logger.error(f"Failed to stop tracking: {e}")
            return False
    
    async def park(self, ha: float = 0.0, dec: float = -90.0) -> bool:
        """
        Park telescope at specified coordinates (async).
        
        Parks the telescope at a safe position for shutdown or maintenance.
        Default park position is on the meridian (HA=0) pointing south.
        
        Args:
            ha: Park hour angle in hours (default: 0.0 - on meridian)
            dec: Park declination in degrees (default: -20.0 - pointing south)
            
        Returns:
            True if parking started successfully
        """
        if not self._initialized:
            logger.error("Telescope not initialized")
            return False
        
        logger.info(f"Parking telescope at HA={ha:.3f}h, Dec={dec:.1f}°")
        
        try:
            async with self._command_lock:
                # Stop tracking if active
                if self.status.state == MountState.TRACKING:
                    logger.info("Stopping tracking before parking")
                    await self.stop_tracking()
                
                # Check if position is safe and reachable
                if not self.coords or not self.coords.is_position_reachable(ha, dec):
                    logger.error(f"Park position not reachable: HA={ha:.4f}h, Dec={dec:.3f}°")
                    return False
                
                if not self._check_slew_safety(ha, dec):
                    logger.error("Park position fails safety checks")
                    return False
                
                # Enable parking mode
                self._parking_mode = True
                self._park_position = (ha, dec)
                self.status.set_state(MountState.PARKING)
                
                # Set conservative motion parameters for parking
                motion_params = self.config.get_motion_params('normal')
                await self._set_motion_parameters(motion_params)
                
                # Execute slew to park position
                success = await self._slew_to_coordinates_async(ha, dec)
                
                if success:
                    logger.info("Parking sequence started - telescope will move to park position")
                    return True
                else:
                    self._parking_mode = False
                    self._park_position = None
                    logger.error("Failed to start parking slew")
                    return False
                
        except Exception as e:
            logger.error(f"Parking failed: {e}")
            self._parking_mode = False
            self._park_position = None
            self.status.set_state(MountState.ERROR)
            return False
    
    async def unpark(self) -> bool:
        """
        Unpark telescope (changes state from PARKED to IDLE).
        
        This is a state change operation - no physical movement occurs.
        The telescope is ready for normal operations after unparking.
        
        Returns:
            True if successful
        """
        if self.status.state != MountState.PARKED:
            logger.warning(f"Cannot unpark: telescope is in {self.status.state.value} state")
            return False
        
        logger.info("Unparking telescope")
        
        try:
            async with self._command_lock:
                # Clear park state
                self._park_position = None
                
                # Change state to idle - telescope is now ready for operations
                self.status.set_state(MountState.IDLE)
                
                logger.info("Telescope unparked - ready for operations")
                return True
                
        except Exception as e:
            logger.error(f"Unpark failed: {e}")
            return False
    
    async def go_to_park_position(self) -> bool:
        """
        Move telescope to the last known park position.
        
        Returns:
            True if slew to park position started successfully
        """
        if not self._park_position:
            logger.error("No park position stored - use park() to set one")
            return False
        
        ha, dec = self._park_position
        logger.info(f"Moving to stored park position: HA={ha:.3f}h, Dec={dec:.1f}°")
        
        return await self.park(ha, dec)
    
    def get_park_position(self) -> Optional[Tuple[float, float]]:
        """
        Get the current park position.
        
        Returns:
            Tuple of (ha, dec) if park position is set, None otherwise
        """
        return self._park_position
    
    def is_parked(self) -> bool:
        """Check if telescope is currently parked"""
        return self.status.state == MountState.PARKED
    
    async def _start_monitoring(self) -> None:
        """Start async status monitoring"""
        if self._monitoring_task is None or self._monitoring_task.done():
            self._monitoring_task = asyncio.create_task(self._monitoring_loop())
            logger.info("Async status monitoring started")
    
    async def _stop_monitoring(self) -> None:
        """Stop async monitoring"""
        self._shutdown_event.set()
        
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Async status monitoring stopped")
    
    async def _monitoring_loop(self) -> None:
        """Async monitoring loop - demonstrates clean concurrent operations"""
        logger.info("Async monitoring loop started")
        
        monitoring_config = self.config.get_monitoring_config()
        update_interval = monitoring_config.get('status_update_interval', 1.0)
        
        while not self._shutdown_event.is_set():
            try:
                # Update telescope status
                await self._update_telescope_status()
                
                # Check for slew completion
                if self.status.state in [MountState.SLEWING, MountState.PARKING]:
                    await self._check_slew_completion()
                
                # Check tracking safety
                if self.status.state == MountState.TRACKING:
                    await self._check_tracking_safety()
                
                # Wait for next update or shutdown signal
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=update_interval)
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Normal timeout, continue monitoring
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in async monitoring loop: {e}")
                await asyncio.sleep(update_interval)
        
        logger.info("Async monitoring loop stopped")
    
    async def _update_telescope_status(self) -> None:
        """Update telescope status from hardware (async)"""
        try:
            # Get status concurrently for better performance
            ra_status1, dec_status1, ra_status2, dec_status2 = await asyncio.gather(
                self.comm.send_command("$Status1RA", "@Status1RA"),
                self.comm.send_command("$Status1Dec", "@Status1Dec"),
                self.comm.send_command("$Status2RA", "@Status2RA"),
                self.comm.send_command("$Status2Dec", "@Status2Dec"),
                return_exceptions=True
            )
            
            # Parse positions
            if isinstance(ra_status1, str) and "," in ra_status1:
                parts = ra_status1.split(",")
                if len(parts) >= 2:
                    cmd_pos = int(float(parts[0].strip()))
                    actual_pos = int(float(parts[1].strip()))
                    self.status.update_axis_from_status1("RA", cmd_pos, actual_pos)
            
            if isinstance(dec_status1, str) and "," in dec_status1:
                parts = dec_status1.split(",")
                if len(parts) >= 2:
                    cmd_pos = int(float(parts[0].strip()))
                    actual_pos = int(float(parts[1].strip()))
                    self.status.update_axis_from_status1("Dec", cmd_pos, actual_pos)
            
            # Update status words
            if isinstance(ra_status2, str):
                self.status.update_axis_from_status2("RA", ra_status2)
            if isinstance(dec_status2, str):
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
            logger.error(f"Async status update failed: {e}")
    
    async def _check_slew_completion(self) -> None:
        """Check if slew has completed (async)"""
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
                await asyncio.gather(
                    self.comm.send_command("$StopRA", "@StopRA"),
                    self.comm.send_command("$StopDec", "@StopDec")
                )
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"Error sending final stop commands: {e}")
            
            # Update state based on mode
            if self._parking_mode:
                logger.info("Parking completed - telescope is now parked")
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
            await self.comm.emergency_stop()
    
    async def _check_tracking_safety(self) -> None:
        """Monitor tracking safety and stop before limits (async)"""
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
                await self.stop()
                
        except Exception as e:
            logger.error(f"Async tracking safety check failed: {e}")
    
    async def _wait_for_slew_completion_async(self, timeout: float, tolerance: int) -> bool:
        """Wait for slew completion with custom parameters (async)"""
        start_time = time.time()
        
        # Store original tolerance and temporarily set new one
        safety_config = self.config.get_safety_config()
        original_tolerance = safety_config.get('position_tolerance_steps', 5000)
        safety_config['position_tolerance_steps'] = tolerance
        
        try:
            while time.time() - start_time < timeout:
                # Check if slew is complete
                if (self._commanded_ha_target is not None and 
                    self._commanded_dec_target is not None):
                    
                    ha_at_target = self._is_axis_at_target("RA")
                    dec_at_target = self._is_axis_at_target("Dec")
                    
                    if ha_at_target and dec_at_target:
                        logger.info("Slew to safe position completed")
                        
                        # Send final stop commands
                        try:
                            await asyncio.gather(
                                self.comm.send_command("$StopRA", "@StopRA"),
                                self.comm.send_command("$StopDec", "@StopDec")
                            )
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            logger.error(f"Error sending final stop commands: {e}")
                        
                        # Clear targets
                        self._commanded_ha_target = None
                        self._commanded_dec_target = None
                        self._slew_start_time = None
                        
                        return True
                
                # Update status and wait
                await self._update_telescope_status()
                await asyncio.sleep(0.5)
            
            logger.error(f"Slew to safe position did not complete within {timeout} seconds")
            return False
            
        finally:
            # Restore original tolerance
            safety_config['position_tolerance_steps'] = original_tolerance
    
    # Helper methods (sync)
    def _calculate_safe_position(self) -> Tuple[float, float]:
        """Calculate safe position away from limits"""
        try:
            # Get current position if available
            current_ha, current_dec = self.get_position()
            
            if current_ha is not None and current_dec is not None:
                # Move to meridian (HA=0) and slightly positive declination
                safe_ha = 0.0  # Hour angle = 0 (on meridian)
                safe_dec = -90  # Move at least 5° from current dec
                
                logger.info(f"Calculated safe position: HA={safe_ha:.3f}h, Dec={safe_dec:.1f}°")
                return safe_ha, safe_dec
            else:
                # Fallback safe position
                logger.warning("Using fallback safe position")
                return 0.0, -90  # Meridian, 15° south
                
        except Exception as e:
            logger.error(f"Error calculating safe position: {e}")
            return 0.0, -90
    
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
    
    def _is_axis_at_target(self, axis: str) -> bool:
        """Check if axis is at commanded target"""
        axis_status = self.status.ra_axis if axis.upper() == "RA" else self.status.dec_axis
        target = self._commanded_ha_target if axis.upper() == "RA" else self._commanded_dec_target
        
        if axis_status.encoder_position is None or target is None:
            return False
        
        error = abs(axis_status.encoder_position - target)
        tolerance = self.config.get_safety_config().get('position_tolerance_steps', 5000)
        
        return error <= tolerance
    
    def get_position(self) -> Tuple[Optional[float], Optional[float]]:
        """Get current telescope position"""
        return self.status.current_hour_angle, self.status.current_declination
    
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
    
    async def __aenter__(self):
        """Async context manager entry"""
        if await self.connect():
            return self
        else:
            raise RuntimeError("Failed to connect to telescope")
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.disconnect()
    
    def __str__(self) -> str:
        """String representation"""
        return (f"AsyncTelescopeDriver(port={self.config.get('serial.port')}, "
                f"state={self.status.state.value}, initialized={self._initialized})")


# Example usage demonstrating async benefits
async def example_usage():
    """
    Example showing the clean async/await syntax and concurrent operations.
    """
    logging.basicConfig(level=logging.INFO)
    
    # Create async driver with context manager
    async with AsyncTelescopeDriver('telescope_config.yaml') as telescope:
        # Initialize telescope
        init_result = await telescope.initialize()
        if not init_result.success:
            print(f"Initialization failed: {init_result.message}")
            return
        
        print(f"Telescope initialized in {init_result.duration_seconds:.1f} seconds")
        
        # Slew to target coordinates
        target_ha, target_dec = 2.0, -30.0  # 2 hours east, 30° south
        
        slew_success = await telescope.slew_to_coordinates(target_ha, target_dec, SlewMode.NORMAL)
        if slew_success:
            print(f"Slewing to HA={target_ha}h, Dec={target_dec}°")
            
            # Monitor position while slewing (concurrent operation)
            while telescope.status.state == MountState.SLEWING:
                await asyncio.sleep(1.0)
                ha, dec = telescope.get_position()
                if ha is not None and dec is not None:
                    print(f"Current position: HA={ha:.3f}h, Dec={dec:.1f}°")
            
            print("Slew completed!")
            
            # Start sidereal tracking
            if await telescope.start_tracking():
                print("Sidereal tracking started")
                
                # Track for 30 seconds
                await asyncio.sleep(30.0)
                
                # Stop tracking
                await telescope.stop()
                print("Tracking stopped")


if __name__ == "__main__":
    # Run async example
    asyncio.run(example_usage())