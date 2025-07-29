import asyncio
import time
import math
from dataclasses import dataclass
from enum import Enum, IntFlag
from typing import Optional, Tuple, List, Callable, Dict, Any
from collections import deque


from comm import Comm
from coordinates import Coordinates
from safety import Safety
from state import MountState, MountStatus, PierSide, TrackingMode


class SlewState(Enum):
    """Enhanced states during slewing operation"""
    IDLE = "idle"
    PREPARING = "preparing"
    SLEWING = "slewing"
    SETTLING = "settling"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"
    RECOVERING = "recovering"
    BELOW_POLE_TRANSITION = "below_pole_transition"


@dataclass
class SlewProgress:
    """Enhanced progress information during slew"""
    state: SlewState = SlewState.IDLE
    start_time: float = 0
    elapsed_time: float = 0
    estimated_remaining: float = 0
    start_ha_enc: int = 0
    start_dec_enc: int = 0
    target_ha_enc: int = 0
    target_dec_enc: int = 0
    current_ha_enc: int = 0
    current_dec_enc: int = 0
    ha_error: int = 0
    dec_error: int = 0
    max_error_seen: int = 0
    progress_percent: float = 0.0
    ha_velocity: float = 0.0
    dec_velocity: float = 0.0
    pier_side: PierSide = PierSide.UNKNOWN


class MountDriver:
    """Complete driver for ROTSE III mount"""

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600, telescope_config: dict = None):
        # Initialize communication layer
        self.comm = Comm(device, baudrate)

        # Initialize status coordinate transformer and safety monitor
        self.status = MountStatus()
        self.coordinates = Coordinates(self.status, telescope_config)
        self.safety = Safety(telescope_config)

        # Configuration from calibration data
        self.config = telescope_config
        self.default_velocity = telescope_config.get('slew_speed', 5000)
        self.position_tolerance = 10  # encoder steps
        self.settling_time = 0.5  # seconds
        self.max_slew_time = 300  # seconds
        self.stall_threshold = 3  # steps/sec
        self.stall_duration = 2.0  # seconds

        # Tracking control
        self._tracking_task = None
        self._safety_monitor_task = None

        # Slew progress tracking
        self.progress = SlewProgress()
        self.progress_callbacks = []
        self.position_history = deque(maxlen=20)
        self.abort_event = asyncio.Event()

    async def connect(self):
        """Initialize connection to the mount"""
        self.status.state = MountState.INITIALIZING
        try:
            # Home the mount to establish position
            await self.home()
            self.status.state = MountState.IDLE
        except Exception as e:
            self.status.state = MountState.ERROR
            raise TelescopeError(f"Mount initialization failed: {e}")

    async def disconnect(self):
        """Cleanly disconnect from the mount"""
        await self.stop_mount()
        self.status.state = MountState.DISCONNECTED

    async def home(self):
        """Home the mount to establish reference position"""
        self.status.state = MountState.HOMING
        try:
            await self.comm.home()
            # Update position after homing
            await self._update_position()
            self.status.state = MountState.IDLE
        except Exception as e:
            self.status.state = MountState.ERROR
            raise TelescopeError(f"Homing failed: {e}")

    async def goto_ha_dec(self, hour_angle: float, declination: float, velocity: Optional[int] = None):
        """Slew to specified equatorial coordinates"""
        self.status.state = MountState.SLEWING

        # Convert coordinates to encoder positions
        ha_enc, dec_enc, below_pole = self.coordinates.ha_dec_to_encoder_positions(hour_angle, declination)
        self.status.pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
        print(ha_enc, dec_enc, below_pole)
        # Execute the slew
        try:
            await self._slew_to_encoder_position(ha_enc, dec_enc, velocity)
            await self._update_position()
            self.status.state = MountState.IDLE
        except Exception as e:
            self.status.state = MountState.ERROR
            raise TelescopeError(f"GOTO failed: {e}")

    async def park(self):
        """Move to park position and disable motors"""
        park_ha = 0  # Point to celestial pole (adjust as needed)
        park_dec = -self.config['observer_latitude']  # Point to celestial pole

        self.status.state = MountState.PARKING
        try:
            await self.goto_ha_dec(park_ha, park_dec)
            await self.comm.stop()
            self.status.state = MountState.PARKED
        except Exception as e:
            self.status.state = MountState.ERROR
            raise TelescopeError(f"Parking failed: {e}")

    async def start_tracking(self, mode: TrackingMode = TrackingMode.SIDEREAL):
        """Start tracking with specified mode"""
        if self.status.state != MountState.IDLE:
            raise TelescopeError("Cannot start tracking from current state")

        self.status.state = MountState.TRACKING
        self.status.tracking_mode = mode

        # Start safety monitor
        self._safety_monitor_task = asyncio.create_task(self._monitor_tracking_safety())

    async def stop_tracking(self):
        """Stop any active tracking"""
        if self._safety_monitor_task:
            self._safety_monitor_task.cancel()
            try:
                await self._safety_monitor_task
            except asyncio.CancelledError:
                pass

        await self.comm.stop()
        self.status.state = MountState.IDLE
        self.status.tracking_mode = TrackingMode.STOPPED

    async def stop_mount(self):
        """Emergency stop all movement"""
        await self.comm.stop()
        if self.status.state == MountState.TRACKING:
            await self.stop_tracking()
        self.status.state = MountState.IDLE

    async def abort_slew(self):
        """Abort current slew operation"""
        self.abort_event.set()
        await self.comm.stop()
        self.progress.state = SlewState.ABORTED
        self._notify_progress()
        self.status.state = MountState.IDLE

    async def get_current_position(self) -> Tuple[float, float]:
        """Get current HA/Dec coordinates"""
        return await self._update_position()

    async def _update_position(self) -> Tuple[float, float]:
        """Update and return current position"""
        ha_enc, dec_enc = await self.comm.get_encoder_positions()
        ha, dec, below_pole = self.coordinates.encoder_positions_to_ha_dec(ha_enc, dec_enc)

        # Update status
        self.status.current_hour_angle = ha
        self.status.current_declination = dec
        self.status.ra_encoder = ha_enc
        self.status.dec_encoder = dec_enc
        self.status.pier_side = PierSide.BELOW_THE_POLE if below_pole else PierSide.NORMAL
        self.status.last_position_update = time.time()

        return ha, dec

    async def _slew_to_encoder_position(self,
                                        target_ha_enc: int,
                                        target_dec_enc: int,
                                        velocity: Optional[int] = None) -> bool:
        """Internal method for slewing to encoder positions"""
        # Reset tracking
        self.abort_event.clear()
        self.position_history.clear()

        # Safety checks
        if not self.safety.enc_position_is_within_safety_limits(target_ha_enc, target_dec_enc):
            raise SafetyError(f"Target position exceeds safety limits")

        # Get current position
        current_ha_enc, current_dec_enc = await self.comm.get_encoder_positions()

        # Initialize progress tracking
        self.progress = SlewProgress(
            state=SlewState.PREPARING,
            start_time=time.time(),
            start_ha_enc=current_ha_enc,
            start_dec_enc=current_dec_enc,
            target_ha_enc=target_ha_enc,
            target_dec_enc=target_dec_enc,
            current_ha_enc=current_ha_enc,
            current_dec_enc=current_dec_enc
        )

        try:
            # Set velocity
            slew_vel = velocity or self.default_velocity
            await self.comm.set_velocity(slew_vel, slew_vel)

            # Start the move
            await self.comm.move_enc(target_ha_enc, target_dec_enc)
            self.progress.state = SlewState.SLEWING
            self._notify_progress()

            # Monitor progress
            await self._monitor_slew_progress()

            return True

        except Exception as e:
            self.progress.state = SlewState.FAILED
            self._notify_progress()
            raise

    async def _monitor_slew_progress(self):
        """Monitor slew progress and handle completion"""
        start_time = time.time()
        consecutive_on_target = 0
        required_consecutive = int(self.settling_time / 0.1)

        while time.time() - start_time < self.max_slew_time:
            if self.abort_event.is_set():
                await self.comm.stop()
                self.progress.state = SlewState.ABORTED
                return False

            # Get current position
            try:
                current_time = time.time()
                ha_enc, dec_enc = await self.comm.get_encoder_positions()
                self.position_history.append((ha_enc, dec_enc, current_time))

                # Update progress
                self.progress.current_ha_enc = ha_enc
                self.progress.current_dec_enc = dec_enc
                self.progress.elapsed_time = current_time - self.progress.start_time

                # Calculate errors
                ha_error = abs(ha_enc - self.progress.target_ha_enc)
                dec_error = abs(dec_enc - self.progress.target_dec_enc)
                self.progress.ha_error = ha_error
                self.progress.dec_error = dec_error

                # Update progress percentage
                start_dist = math.hypot(
                    self.progress.target_ha_enc - self.progress.start_ha_enc,
                    self.progress.target_dec_enc - self.progress.start_dec_enc
                )
                if start_dist > 0:
                    current_dist = math.hypot(
                        self.progress.target_ha_enc - ha_enc,
                        self.progress.target_dec_enc - dec_enc
                    )
                    self.progress.progress_percent = max(0, min(100, 100 * (1 - current_dist / start_dist)))

                # Check if on target
                if ha_error <= self.position_tolerance and dec_error <= self.position_tolerance:
                    consecutive_on_target += 1
                    if consecutive_on_target >= required_consecutive:
                        self.progress.state = SlewState.COMPLETE
                        self._notify_progress()
                        return True
                else:
                    consecutive_on_target = 0

                self._notify_progress()
                await asyncio.sleep(0.1)

            except Exception as e:
                print(f"Position read error: {e}")
                await asyncio.sleep(0.1)
                continue

        # Timeout reached
        await self.comm.stop()
        raise PositionError(f"Slew timeout after {self.max_slew_time}s")

    async def _monitor_tracking_safety(self):
        """Monitor mount position during tracking for safety limits"""
        while self.status.state == MountState.TRACKING:
            try:
                ha_enc, dec_enc = await self.comm.get_encoder_positions()
                if not self.safety.enc_position_is_within_safety_limits(ha_enc, dec_enc):
                    print("Safety limit reached during tracking!")
                    await self.stop_mount()
                    break
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Safety monitor error: {e}")
                await self.stop_mount()
                break

    def add_progress_callback(self, callback: Callable[[SlewProgress], None]):
        """Add a callback for slew progress updates"""
        self.progress_callbacks.append(callback)

    def _notify_progress(self):
        """Notify all registered callbacks of progress updates"""
        for callback in self.progress_callbacks:
            try:
                callback(self.progress)
            except Exception as e:
                print(f"Progress callback error: {e}")


class TelescopeError(Exception):
    """Base class for telescope-related errors"""
    pass


class SafetyError(TelescopeError):
    """Error raised when safety limits are violated"""
    pass


class PositionError(TelescopeError):
    """Error related to position or movement"""
    pass


class MountFaultError(TelescopeError):
    """Error indicating a mount hardware fault"""
    pass