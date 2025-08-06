import asyncio
import time
import logging
from typing import Optional, Tuple, Dict, Any
from dataclasses import replace

from comm import Comm
from coordinates import Coordinates
from safety import Safety
from state import MountStatus, MountState, TrackingMode, PierSide

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TelescopeMount:
    """
    Complete telescope mount controller for fork-mounted equatorial telescope

    Features:
    - Velocity-based tracking (sidereal and non-sidereal)
    - Thread-safe operations with proper locking
    - Comprehensive safety monitoring
    - Fork mount coordinate handling with below-pole pointing
    - Slewing with automatic tracking resume
    - Emergency stop functionality
    """

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600, calibration_data: Dict[str, Any] = None):
        if calibration_data is None:
            raise ValueError("calibration_data is required for mount operation")

        # Validate required calibration data
        required_keys = ['limits', 'ranges', 'dec_steps_per_degree',
                         'observer_latitude', 'sidereal_rate_ha_steps_per_sec']
        for key in required_keys:
            if key not in calibration_data:
                raise ValueError(f"Missing required calibration data: {key}")

        # Initialize core components
        self.comm = Comm(device, baudrate)
        self.status = MountStatus()
        self.coordinates = Coordinates(self.status, calibration_data)
        self.safety = Safety(calibration_data)

        # Thread synchronization
        self._command_lock = asyncio.Lock()  # Serialize all mount commands
        self._status_lock = asyncio.Lock()  # Protect status updates
        self._position_lock = asyncio.Lock()  # Protect position updates

        # Configuration parameters
        self._sidereal_rate_ha = calibration_data.get('sidereal_rate_ha_steps_per_sec', 100)
        self._position_update_interval = calibration_data.get('position_update_interval', 0.1)
        self._slew_tolerance_steps = calibration_data.get('slew_tolerance_steps', 10)
        self._max_slew_time = calibration_data.get('max_slew_time_seconds', 300)
        self._tracking_safety_buffer = calibration_data.get('tracking_safety_buffer_steps', 5000)

        # Operational tasks
        self._position_monitor_task = None
        self._safety_monitor_task = None
        self._tracking_monitor_task = None

        # Movement detection
        self._last_position = (None, None)
        self._stationary_count = 0
        self._stationary_threshold = 5

        # Mount limits from calibration
        limits = calibration_data['limits']
        self._ha_pos_lim = limits['ha_positive']
        self._ha_neg_lim = limits['ha_negative']
        self._dec_pos_lim = limits['dec_positive']
        self._dec_neg_lim = limits['dec_negative']

        # Homing initialisation calibration data
        self._home_timeout = calibration_data.get('home_timeout_seconds', 120)
        self._ha_tolerance = calibration_data.get('home_position_tolerance_ha', 500)
        self._dec_tolerance = calibration_data.get('home_position_tolerance_dec', 500)

    # ================================
    #           MANAGEMENT
    # ================================

    async def initialize(self) -> bool:
        """
        Initialize the mount with enhanced verification sequence:
        1. Home both axes
        2. Verify encoder positions match expected home positions
        3. Move to axis midpoints
        4. Start monitoring tasks
        """
        async with self._command_lock:
            try:
                await self._set_state(MountState.INITIALIZING)
                logger.info("Starting enhanced telescope mount initialization...")

                # Step 1: Home the telescope and verify positions
                if not await self._home_and_verify():
                    logger.error("Homing and verification failed")
                    await self._set_state(MountState.ERROR)
                    return False

                # Step 2: Move to axis midpoints for safe starting position
                if not await self._move_to_midpoints():
                    logger.error("Failed to move to axis midpoints")
                    await self._set_state(MountState.ERROR)
                    return False

                # Step 3: Start monitoring tasks
                self._position_monitor_task = asyncio.create_task(self._position_monitor())
                self._safety_monitor_task = asyncio.create_task(self._safety_monitor())

                await self._set_state(MountState.IDLE)
                logger.info("Enhanced mount initialization completed successfully")
                return True

            except Exception as e:
                logger.error(f"Mount initialization failed: {e}")
                await self._set_state(MountState.ERROR)
                return False

    async def _home_and_verify(self) -> bool:
        """
        Home both axes and verify encoder positions match expected home positions.

        Returns:
            bool: True if homing successful and positions verified
        """
        logger.info("Homing telescope and verifying encoder positions...")

        try:
            # Stop any current movement
            await self.comm.stop()
            await asyncio.sleep(1.0)

            # Execute homing sequence
            await self.comm.home()

            # Wait for homing to complete with timeout
            home_timeout = self._home_timeout
            if not await self._wait_for_homing_complete(home_timeout):
                logger.error("Homing operation timed out")
                return False

            # Get current encoder positions after homing
            await self._update_encoder_positions()
            current_ha_enc = self.status.ra_encoder
            current_dec_enc = self.status.dec_encoder

            if current_ha_enc is None or current_dec_enc is None:
                logger.error("Failed to read encoder positions after homing")
                return False

            # Verify positions match expected home positions
            expected_home_ha, expected_home_dec = self._get_expected_home_positions()

            if not self._verify_home_positions(current_ha_enc, current_dec_enc,
                                               expected_home_ha, expected_home_dec):
                logger.error("Encoder positions do not match expected home positions")
                return False

            logger.info(f"Homing verified - HA: {current_ha_enc}, Dec: {current_dec_enc}")
            return True

        except Exception as e:
            logger.error(f"Homing and verification failed: {e}")
            return False

    def _get_expected_home_positions(self) -> Tuple[int, int]:
        """
        Get expected encoder positions when at home position.

        For ROTSE III mounts, home is typically at:
        - HA: Center of travel range (pointing south)
        - Dec: Horizontal position (pointing at horizon)

        Returns:
            Tuple[int, int]: Expected (ha_encoder, dec_encoder) at home
        """
        # HA home position: center of travel range
        ha_home = (self._ha_pos_lim + self._ha_neg_lim) // 2

        # Dec home position: horizontal (declination = observer_latitude - 90)
        # This points the telescope horizontally
        observer_lat = self.coordinates._observer_latitude
        home_declination = observer_lat - 90  # Horizontal pointing

        # Convert to encoder position using coordinates class
        # We'll use a dummy HA value since we only care about Dec
        _, dec_home, _ = self.coordinates.ha_dec_to_encoder_positions(0, home_declination)

        return ha_home, dec_home

    def _verify_home_positions(self, actual_ha: int, actual_dec: int,
                               expected_ha: int, expected_dec: int) -> bool:
        """
        Verify that actual encoder positions are within tolerance of expected home positions.

        Args:
            actual_ha: Actual HA encoder position
            actual_dec: Actual Dec encoder position
            expected_ha: Expected HA encoder position at home
            expected_dec: Expected Dec encoder position at home

        Returns:
            bool: True if positions are within acceptable tolerance
        """
        # Tolerance for home position verification (encoder steps)
        ha_tolerance = self._ha_tolerance
        dec_tolerance = self._dec_tolerance

        ha_error = abs(actual_ha - expected_ha)
        dec_error = abs(actual_dec - expected_dec)

        ha_ok = ha_error <= ha_tolerance
        dec_ok = dec_error <= dec_tolerance

        logger.info(f"Home position verification:")
        logger.info(
            f"  HA: actual={actual_ha}, expected={expected_ha}, error={ha_error}, tolerance={ha_tolerance}, OK={ha_ok}")
        logger.info(
            f"  Dec: actual={actual_dec}, expected={expected_dec}, error={dec_error}, tolerance={dec_tolerance}, OK={dec_ok}")

        if not ha_ok:
            logger.error(f"HA encoder position error {ha_error} exceeds tolerance {ha_tolerance}")
        if not dec_ok:
            logger.error(f"Dec encoder position error {dec_error} exceeds tolerance {dec_tolerance}")

        return ha_ok and dec_ok

    async def _wait_for_homing_complete(self, timeout_seconds: float) -> bool:
        """
        Wait for homing operation to complete by monitoring mount status.

        Args:
            timeout_seconds: Maximum time to wait for homing

        Returns:
            bool: True if homing completed within timeout
        """
        start_time = time.time()
        last_position = (None, None)
        stable_count = 0
        required_stable_readings = 5  # Require 5 stable readings to confirm stopped

        logger.info(f"Waiting for homing to complete (timeout: {timeout_seconds}s)")

        while time.time() - start_time < timeout_seconds:
            try:
                # Get current positions
                ra_enc, dec_enc = await self.comm.get_encoder_positions()
                current_position = (ra_enc, dec_enc)

                # Check if position has stabilized
                if current_position == last_position:
                    stable_count += 1
                    if stable_count >= required_stable_readings:
                        logger.info("Homing completed - mount position stabilized")
                        return True
                else:
                    stable_count = 0

                last_position = current_position

                # Log progress every 10 seconds
                elapsed = time.time() - start_time
                if int(elapsed) % 10 == 0 and elapsed > 0:
                    logger.info(f"Homing in progress... {elapsed:.0f}s elapsed, position: HA={ra_enc}, Dec={dec_enc}")

            except Exception as e:
                logger.warning(f"Error checking homing status: {e}")

            await asyncio.sleep(1.0)

        logger.error("Homing operation timed out")
        return False

    async def _move_to_midpoints(self) -> bool:
        """
        Move telescope to axis midpoints as safe starting position.

        Returns:
            bool: True if successfully moved to midpoints
        """
        logger.info("Moving to axis midpoints for safe starting position...")

        try:
            # Calculate midpoint positions
            ha_midpoint = (self._ha_pos_lim + self._ha_neg_lim) // 2
            dec_midpoint = (self._dec_pos_lim + self._dec_neg_lim) // 2

            # Convert to HA/Dec coordinates for logging
            ha_hours, dec_degrees, below_pole = self.coordinates.encoder_positions_to_ha_dec(
                ha_midpoint, dec_midpoint)

            logger.info(f"Moving to midpoints: HA={ha_hours:.2f}h, Dec={dec_degrees:.1f}°")
            logger.info(f"Encoder positions: HA_enc={ha_midpoint}, Dec_enc={dec_midpoint}")

            # Set reasonable slew speeds for initialization move
            init_speed_ha = 30000
            init_speed_dec = 30000
            await self.comm.set_velocity(init_speed_ha, init_speed_dec)

            # Execute move to midpoints
            await self.comm.move_enc(ha_midpoint, dec_midpoint)

            # Wait for move to complete
            move_timeout = 180
            if not await self._wait_for_position_reached(ha_midpoint, dec_midpoint, move_timeout):
                logger.error("Failed to reach axis midpoints within timeout")
                return False

            # Update status with final position
            await self._update_encoder_positions()

            logger.info("Successfully moved to axis midpoints")
            logger.info(
                f"Final position: HA={self.status.current_hour_angle:.2f}h, Dec={self.status.current_declination:.1f}°")

            return True

        except Exception as e:
            logger.error(f"Failed to move to axis midpoints: {e}")
            return False

    async def _wait_for_position_reached(self, target_ha: int, target_dec: int,
                                         timeout_seconds: float) -> bool:
        """
        Wait for telescope to reach target encoder positions.

        Args:
            target_ha: Target HA encoder position
            target_dec: Target Dec encoder position
            timeout_seconds: Maximum time to wait

        Returns:
            bool: True if target position reached within timeout
        """
        start_time = time.time()
        tolerance = self._slew_tolerance_steps

        while time.time() - start_time < timeout_seconds:
            try:
                # Update current position
                await self._update_encoder_positions()

                if self.status.ra_encoder is None or self.status.dec_encoder is None:
                    await asyncio.sleep(0.5)
                    continue

                # Check if we've reached the target
                ha_error = abs(self.status.ra_encoder - target_ha)
                dec_error = abs(self.status.dec_encoder - target_dec)

                if ha_error <= tolerance and dec_error <= tolerance:
                    logger.info(f"Target position reached - HA error: {ha_error}, Dec error: {dec_error}")
                    return True

                # Log progress every 10 seconds
                elapsed = time.time() - start_time
                if int(elapsed) % 10 == 0 and elapsed > 0:
                    logger.info(f"Moving to target... {elapsed:.0f}s elapsed, errors: HA={ha_error}, Dec={dec_error}")

            except Exception as e:
                logger.warning(f"Error checking position: {e}")

            await asyncio.sleep(0.5)

        logger.error("Target position not reached within timeout")
        return False

    async def shutdown(self):
        """Shutdown the mount and cleanup tasks"""
        async with self._command_lock:
            logger.info("Shutting down telescope mount...")

            # Stop tracking
            await self._stop_tracking_internal()

            # Stop all motors
            await self.comm.stop()

            # Cancel monitoring tasks
            tasks = [self._position_monitor_task, self._safety_monitor_task, self._tracking_monitor_task]
            for task in tasks:
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            await self._set_state(MountState.DISCONNECTED)
            logger.info("Mount shutdown complete")

    async def home(self) -> bool:
        """Home the telescope mount"""
        try:
            # Acquire lock with timeout
            await asyncio.wait_for(self._command_lock.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Homing timed out waiting for command lock")
            return False

        try:
            if self.status.state not in [MountState.IDLE, MountState.ERROR]:
                logger.warning("Cannot home: mount not in idle state")
                return False

            # Stop any tracking first
            await self._stop_tracking_internal()

            await self._set_state(MountState.HOMING)
            logger.info("Homing telescope mount...")

            await self.comm.home()

            # Wait for homing to complete
            await asyncio.sleep(60.0)  # TODO: Implement proper homing detection

            await self._update_encoder_positions()
            await self._set_state(MountState.IDLE)
            logger.info("Homing complete")
            return True

        except Exception as e:
            logger.error(f"Homing failed: {e}")
            await self._set_state(MountState.ERROR)
            return False
        finally:
            self._command_lock.release()

    async def emergency_stop(self):
        """Emergency stop - highest priority, bypasses most locks"""
        logger.warning("EMERGENCY STOP activated")

        # Don't wait for command lock in emergency
        try:
            await asyncio.wait_for(self.comm.stop(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.error("Emergency stop communication timed out!")

        # Cancel tracking monitor
        if self._tracking_monitor_task and not self._tracking_monitor_task.done():
            self._tracking_monitor_task.cancel()

        # Update status safely
        async with self._status_lock:
            self.status.tracking_mode = TrackingMode.STOPPED
            self.status.is_moving = False
            self.status.state = MountState.IDLE

    # ================================
    #           SLEWING
    # ================================

    async def slew_to_ha_dec(self, target_ha: float, target_dec: float) -> bool:
        """
        Slew to specified hour angle and declination

        Args:
            target_ha: Target hour angle in hours (-12 to +12)
            target_dec: Target declination in degrees (-90 to +90)
        """
        try:
            # Acquire lock with timeout
            await asyncio.wait_for(self._command_lock.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Slew command timed out waiting for command lock")
            return False

        try:
            if self.status.state not in [MountState.IDLE, MountState.TRACKING]:
                logger.warning(f"Cannot slew: mount in state {self.status.state}")
                return False

            # Remember tracking state
            was_tracking = self.status.tracking_mode != TrackingMode.STOPPED
            tracking_mode = self.status.tracking_mode

            # Stop tracking first
            await self._stop_tracking_internal()

            # Convert to encoder positions
            target_ha_enc, target_dec_enc, below_pole = self.coordinates.ha_dec_to_encoder_positions(
                target_ha, target_dec)

            # Safety check
            if not self.safety.enc_position_is_within_safety_limits(target_ha_enc, target_dec_enc):
                logger.error(f"Slew target outside safety limits: HA={target_ha:.2f}h, Dec={target_dec:.2f}°")
                return False

            # Update target position
            await self._update_status(
                target_hour_angle=target_ha,
                target_declination=target_dec,
                target_ra_encoder=target_ha_enc,
                target_dec_encoder=target_dec_enc,
                pier_side=PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
            )

            # Execute slew
            success = await self._execute_slew(target_ha_enc, target_dec_enc)

            # Restart tracking if it was on before
            if success and was_tracking:
                if tracking_mode == TrackingMode.SIDEREAL:
                    await asyncio.sleep(0.5)  # Let mount settle
                    await self.start_sidereal_tracking()

            return success

        except Exception as e:
            logger.error(f"Slew failed: {e}")
            await self._set_state(MountState.ERROR)
            return False
        finally:
            self._command_lock.release()

    async def _execute_slew(self, target_ha_enc: int, target_dec_enc: int) -> bool:
        """Execute the actual slew movement"""
        await self._set_state(MountState.SLEWING)
        self.status.slew_start_time = time.time()
        await self._update_status(is_moving=True)

        logger.info(f"Slewing to HA_enc={target_ha_enc}, Dec_enc={target_dec_enc}")

        try:
            # Stop all movement
            await self.comm.stop()

            # Set slew speeds
            slew_speed_ha = 50000  # steps/sec
            slew_speed_dec = 50000
            await self.comm.set_velocity(slew_speed_ha, slew_speed_dec)

            # Start the slew
            await self.comm.move_enc(target_ha_enc, target_dec_enc)

            # Wait for slew to complete
            slew_complete = await self._wait_for_slew_completion(target_ha_enc, target_dec_enc)

            if slew_complete:
                logger.info("Slew completed successfully")
                await self._set_state(MountState.IDLE)
                await self._update_status(is_moving=False)
                return True
            else:
                logger.error("Slew timed out or failed")
                await self.comm.stop()
                await self._set_state(MountState.ERROR)
                await self._update_status(is_moving=False)
                return False

        except Exception as e:
            logger.error(f"Slew execution failed: {e}")
            await self.comm.stop()
            await self._set_state(MountState.ERROR)
            await self._update_status(is_moving=False)
            return False

    async def _wait_for_slew_completion(self, target_ha_enc: int, target_dec_enc: int) -> bool:
        """Wait for slew to complete within tolerance"""
        timeout = self._max_slew_time
        start_time = time.time()

        while time.time() - start_time < timeout:
            await self._update_encoder_positions()

            if (self.status.ra_encoder is not None and self.status.dec_encoder is not None):
                ha_error = abs(self.status.ra_encoder - target_ha_enc)
                dec_error = abs(self.status.dec_encoder - target_dec_enc)

                if ha_error <= self._slew_tolerance_steps and dec_error <= self._slew_tolerance_steps:
                    # Wait a bit more to ensure mount has settled
                    await asyncio.sleep(0.5)
                    return True

            await asyncio.sleep(0.2)

        return False

    # ================================
    # TRACKING OPERATIONS
    # ================================

    async def start_sidereal_tracking(self) -> bool:
        """Start sidereal tracking using velocity-based method"""
        try:
            # Acquire lock with timeout
            await asyncio.wait_for(self._command_lock.acquire(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Start tracking timed out waiting for command lock")
            return False

        try:
            if self.status.state not in [MountState.IDLE, MountState.TRACKING]:
                logger.warning(f"Cannot start tracking: mount in state {self.status.state}")
                return False

            # Stop any existing movement
            await self.comm.stop()

            # Determine tracking direction based on pier side
            if self.status.pier_side == PierSide.BELOW_THE_POLE:
                # Below pole: track eastward (negative HA direction)
                tracking_rate = -self._sidereal_rate_ha
                distant_ha_target = self._ha_pos_lim + 1000000
                logger.info("Starting sidereal tracking (below-pole mode: eastward)")
            else:
                # Normal: track westward (positive HA direction)
                tracking_rate = self._sidereal_rate_ha
                distant_ha_target = self._ha_neg_lim - 1000000
                logger.info("Starting sidereal tracking (normal mode: westward)")

            # Set sidereal velocity (HA only, Dec = 0)
            await self.comm.set_velocity(int(abs(tracking_rate)), 0)

            # Set HA target far beyond limit to track continuously
            await self.comm.move_ra_enc(distant_ha_target)

            # Start tracking monitor
            if self._tracking_monitor_task and not self._tracking_monitor_task.done():
                self._tracking_monitor_task.cancel()

            self._tracking_monitor_task = asyncio.create_task(
                self._velocity_tracking_monitor(tracking_rate, 0.0)
            )

            await self._update_status(tracking_mode=TrackingMode.SIDEREAL)
            await self._set_state(MountState.TRACKING)

            logger.info(
                f"Sidereal tracking started - Rate: {tracking_rate:.1f} steps/sec, Pier side: {self.status.pier_side.value}")
            return True

        except Exception as e:
            logger.error(f"Failed to start sidereal tracking: {e}")
            await self._set_state(MountState.ERROR)
            return False
        finally:
            self._command_lock.release()

    async def start_non_sidereal_tracking(self, ha_rate_steps_per_sec: float, dec_rate_steps_per_sec: float) -> bool:
        """
        Start non-sidereal tracking with custom rates

        Args:
            ha_rate_steps_per_sec: HA tracking rate in steps per second (positive = westward)
            dec_rate_steps_per_sec: Dec tracking rate in steps per second (positive = northward)
        """
        try:
            # Acquire lock with timeout
            await asyncio.wait_for(self._command_lock.acquire(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Start tracking timed out waiting for command lock")
            return False

        try:
            if self.status.state not in [MountState.IDLE, MountState.TRACKING]:
                logger.warning(f"Cannot start tracking: mount in state {self.status.state}")
                return False

            logger.info(
                f"Starting non-sidereal tracking: HA={ha_rate_steps_per_sec:.2f}, Dec={dec_rate_steps_per_sec:.2f} steps/sec")

            # Stop any existing movement
            await self.comm.stop()

            # Set custom tracking velocities
            await self.comm.set_velocity(int(abs(ha_rate_steps_per_sec)), int(abs(dec_rate_steps_per_sec)))

            # Set targets based on rate directions - far beyond limits for continuous movement
            if ha_rate_steps_per_sec >= 0:
                ha_target = self._ha_pos_lim + 1000000  # Track westward (positive)
            else:
                ha_target = self._ha_neg_lim - 1000000  # Track eastward (negative)

            if dec_rate_steps_per_sec >= 0:
                dec_target = self._dec_pos_lim + 1000000  # Track northward
            else:
                dec_target = self._dec_neg_lim - 1000000  # Track southward

            # Start tracking movement
            if dec_rate_steps_per_sec == 0:
                # HA-only tracking
                await self.comm.move_ra_enc(ha_target)
            else:
                # Both axes tracking
                await self.comm.move_enc(ha_target, dec_target)

            # Start tracking monitor
            if self._tracking_monitor_task and not self._tracking_monitor_task.done():
                self._tracking_monitor_task.cancel()

            self._tracking_monitor_task = asyncio.create_task(
                self._velocity_tracking_monitor(ha_rate_steps_per_sec, dec_rate_steps_per_sec)
            )

            await self._update_status(tracking_mode=TrackingMode.NON_SIDEREAL)
            await self._set_state(MountState.TRACKING)

            logger.info("Non-sidereal tracking started")
            return True

        except Exception as e:
            logger.error(f"Failed to start non-sidereal tracking: {e}")
            await self._set_state(MountState.ERROR)
            return False
        finally:
            self._command_lock.release()

    async def stop_tracking(self):
        """Public interface to stop tracking"""
        async with self._command_lock:
            await self._stop_tracking_internal()

    async def _stop_tracking_internal(self):
        """Internal stop tracking without acquiring command lock"""
        if self.status.tracking_mode != TrackingMode.STOPPED:
            logger.info("Stopping tracking")

            # Cancel tracking monitor task
            if self._tracking_monitor_task and not self._tracking_monitor_task.done():
                self._tracking_monitor_task.cancel()
                try:
                    await self._tracking_monitor_task
                except asyncio.CancelledError:
                    pass

            # Stop all motors
            await self.comm.stop()

            await self._update_status(tracking_mode=TrackingMode.STOPPED)
            if self.status.state == MountState.TRACKING:
                await self._set_state(MountState.IDLE)

    async def _velocity_tracking_monitor(self, ha_rate: float, dec_rate: float):
        """Monitor velocity-based tracking for safety"""
        try:
            logger.info(f"Starting velocity tracking monitor: HA={ha_rate:.2f}, Dec={dec_rate:.2f} steps/sec")

            while True:
                await asyncio.sleep(2.0)  # Check every 2 seconds

                # Check if we should still be tracking
                if self.status.tracking_mode == TrackingMode.STOPPED:
                    logger.info("Tracking monitor stopping - tracking mode is STOPPED")
                    break

                # Get current position safely
                async with self._position_lock:
                    current_ha = self.status.ra_encoder
                    current_dec = self.status.dec_encoder

                if current_ha is None or current_dec is None:
                    logger.warning("Tracking monitor: no position data available")
                    continue

                # Check if we're approaching safety limits
                if not self._is_safe_for_continued_tracking(current_ha, current_dec, ha_rate, dec_rate):
                    logger.warning("Tracking stopped: approaching safety limits")
                    await self._stop_tracking_due_to_limits()
                    break

        except asyncio.CancelledError:
            logger.info("Velocity tracking monitor cancelled")
        except Exception as e:
            logger.error(f"Velocity tracking monitor error: {e}")
            await self._set_state(MountState.ERROR)

    def _is_safe_for_continued_tracking(self, current_ha: int, current_dec: int,
                                        ha_rate: float, dec_rate: float) -> bool:
        """Check if it's safe to continue tracking in the current direction"""

        # Check HA direction and limits
        if ha_rate > 0:  # Moving toward positive limit (westward)
            ha_safe = current_ha < (self._ha_pos_lim - self._tracking_safety_buffer)
        elif ha_rate < 0:  # Moving toward negative limit (eastward)
            ha_safe = current_ha > (self._ha_neg_lim + self._tracking_safety_buffer)
        else:  # Not moving in HA
            ha_safe = ((self._ha_neg_lim + self._tracking_safety_buffer) < current_ha <
                       (self._ha_pos_lim - self._tracking_safety_buffer))

        # Check Dec direction and limits
        if dec_rate > 0:  # Moving toward positive limit (northward)
            dec_safe = current_dec < (self._dec_pos_lim - self._tracking_safety_buffer)
        elif dec_rate < 0:  # Moving toward negative limit (southward)
            dec_safe = current_dec > (self._dec_neg_lim + self._tracking_safety_buffer)
        else:  # Not moving in Dec
            dec_safe = ((self._dec_neg_lim + self._tracking_safety_buffer) < current_dec <
                        (self._dec_pos_lim - self._tracking_safety_buffer))

        return ha_safe and dec_safe

    async def _stop_tracking_due_to_limits(self):
        """Stop tracking because we're approaching limits"""
        logger.warning("Stopping tracking due to approaching limits")

        # Don't wait for command lock - this is safety-related
        try:
            await asyncio.wait_for(self.comm.stop(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.error("Failed to stop tracking due to timeout")

        await self._update_status(tracking_mode=TrackingMode.STOPPED)
        if self.status.state == MountState.TRACKING:
            await self._set_state(MountState.IDLE)

    # ================================
    # MONITORING TASKS
    # ================================

    async def _position_monitor(self):
        """Continuously monitor mount position"""
        try:
            while True:
                async with self._position_lock:
                    await self._update_encoder_positions()
                    await self._check_movement()
                await asyncio.sleep(self._position_update_interval)
        except asyncio.CancelledError:
            logger.info("Position monitoring stopped")
        except Exception as e:
            logger.error(f"Position monitoring error: {e}")

    async def _safety_monitor(self):
        """Continuously monitor safety limits"""
        try:
            while True:
                # Read position safely
                async with self._position_lock:
                    ra_enc = self.status.ra_encoder
                    dec_enc = self.status.dec_encoder

                if ra_enc is not None and dec_enc is not None:
                    if not self.safety.enc_position_is_within_safety_limits(ra_enc, dec_enc):
                        logger.error("SAFETY LIMIT VIOLATION - Emergency stop activated!")
                        await self.emergency_stop()
                        await self._set_state(MountState.ERROR)
                        break

                await asyncio.sleep(0.1)  # Check safety frequently
        except asyncio.CancelledError:
            logger.info("Safety monitoring stopped")
        except Exception as e:
            logger.error(f"Safety monitoring error: {e}")

    async def _update_encoder_positions(self):
        """Update current encoder positions (call with position_lock held)"""
        try:
            ra_enc, dec_enc = await self.comm.get_encoder_positions()

            self.status.ra_encoder = ra_enc
            self.status.dec_encoder = dec_enc
            self.status.last_position_update = time.time()

            # Convert to HA/Dec
            ha, dec, below_pole = self.coordinates.encoder_positions_to_ha_dec(ra_enc, dec_enc)
            self.status.current_hour_angle = ha
            self.status.current_declination = dec
            self.status.pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL

        except Exception as e:
            logger.error(f"Failed to update encoder positions: {e}")

    async def _check_movement(self):
        """Check if mount is currently moving (call with position_lock held)"""
        current_pos = (self.status.ra_encoder, self.status.dec_encoder)

        if current_pos == self._last_position:
            self._stationary_count += 1
        else:
            self._stationary_count = 0

        # Update movement status based on current state
        if self.status.state == MountState.SLEWING:
            self.status.is_moving = True
        elif self.status.tracking_mode != TrackingMode.STOPPED:
            self.status.is_moving = True
        else:
            self.status.is_moving = self._stationary_count < self._stationary_threshold

        self._last_position = current_pos

    # ================================
    # STATUS AND UTILITY METHODS
    # ================================

    async def _set_state(self, new_state: MountState):
        """Thread-safe state change with logging"""
        async with self._status_lock:
            if self.status.state != new_state:
                old_state = self.status.state
                self.status.state = new_state
                logger.info(f"Mount state changed: {old_state.value} -> {new_state.value}")

    async def _update_status(self, **kwargs):
        """Thread-safe status updates"""
        async with self._status_lock:
            for key, value in kwargs.items():
                if hasattr(self.status, key):
                    setattr(self.status, key, value)

    def get_status(self) -> MountStatus:
        """Get current mount status (thread-safe copy)"""
        return replace(self.status)

    def is_tracking(self) -> bool:
        """Check if mount is currently tracking"""
        return self.status.tracking_mode != TrackingMode.STOPPED

    def is_slewing(self) -> bool:
        """Check if mount is currently slewing"""
        return self.status.state == MountState.SLEWING

    def is_idle(self) -> bool:
        """Check if mount is idle and ready for commands"""
        return self.status.state == MountState.IDLE

    async def wait_for_state(self, target_state: MountState, timeout: float = 30.0) -> bool:
        """Wait for the mount to reach a specific state"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.status.state == target_state:
                return True
            await asyncio.sleep(0.1)
        return False

    def get_current_position(self) -> Tuple[Optional[float], Optional[float]]:
        """Get current HA/Dec position in degrees"""
        return self.status.current_hour_angle, self.status.current_declination

    def get_target_position(self) -> Tuple[Optional[float], Optional[float]]:
        """Get target HA/Dec position in degrees"""
        return self.status.target_hour_angle, self.status.target_declination