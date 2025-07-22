import asyncio
from datetime import datetime
from typing import Dict, Tuple, Optional, Callable
from enum import Enum
from dataclasses import dataclass


class CalibrationStatus(Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CalibrationPhase(Enum):
    IDLE = "idle"
    FINDING_HA_NEG = "finding_ra_negative"
    FINDING_HA_POS = "finding_ra_positive"
    FINDING_DEC_NEG = "finding_dec_negative"
    FINDING_DEC_POS = "finding_dec_positive"
    COMPLETE = "complete"

@dataclass
class CalibrationProgress:
    status: CalibrationStatus
    phase: CalibrationPhase
    progress_percent: float
    current_operation: str
    error_message: Optional[str] = None
    limits_found: Dict[str, Optional[int]] = None


class Calibration:
    """Calibration class for telescope."""

    def __init__(self, comm):
        """
        Initialize calibration module.

        Args:
            comm: Object with async methods: get_encoder_positions(),
                           move_ha(), move_dec(), move(), stop(), set_velocity()
        """
        self.mount = comm

        self._calibration_data = {
            'limits': {'ra_negative': None, 'ra_positive': None,
                       'dec_negative': None, 'dec_positive': None},
            'ranges': {'ra_encoder_range': None, 'dec_encoder_range': None},
            'calibrated': False,
            'calibration_date': None,
            'home_ha': None,
            'home_dec': None
        }

        self._status = CalibrationStatus.NOT_STARTED
        self._phase = CalibrationPhase.IDLE
        self._progress = 0.0
        self._current_operation = "Ready"
        self._error_message = None

        # Configurable parameters
        self.motion_timeout = 120.0
        self.stability_threshold = 50  # encoder steps
        self.stability_checks = 5
        self.fast_velocity = 70000
        self.slow_velocity = 5000
        self.home_velocity = 65000
        self.positioning_velocity = 50000

    @property
    def is_calibrated(self) -> bool:
        """Check if telescope is calibrated."""
        return self._calibration_data['calibrated']

    @property
    def calibration_data(self) -> Dict:
        """Get copy of calibration data."""
        return self._calibration_data.copy()

    def get_progress(self) -> CalibrationProgress:
        """Get current calibration progress."""
        return CalibrationProgress(
            status=self._status,
            phase=self._phase,
            progress_percent=self._progress,
            current_operation=self._current_operation,
            error_message=self._error_message,
            limits_found=self._calibration_data['limits'].copy()
        )

    async def wait_for_motion_stop(self, axis: str, timeout: float = None) -> bool:
        """Wait for axis to stop moving by detecting position stability."""
        if timeout is None:
            timeout = self.motion_timeout

        start_time = asyncio.get_event_loop().time()
        last_pos = None
        stable_count = 0

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            ra_enc, dec_enc = await self.mount.get_encoder_positions()
            current_pos = ra_enc if axis.upper() == 'RA' else dec_enc

            if last_pos is not None:
                movement = abs(current_pos - last_pos)
                if movement < self.stability_threshold:
                    stable_count += 1
                    if stable_count >= self.stability_checks:
                        return True
                else:
                    stable_count = 0

            last_pos = current_pos
            await asyncio.sleep(1.0)

        return False

    async def find_limit(self, direction: str, axis: str) -> int:
        """
        Find limit switch position for given axis and direction.

        Args:
            direction: 'positive' or 'negative'
            axis: 'RA' or 'DEC'

        Returns:
            Encoder position at limit

        Raises:
            RuntimeError: If limit finding fails
        """
        self._current_operation = f"Finding {axis} {direction} limit"

        # Stop current motion
        await self.mount.stop()
        await asyncio.sleep(1)

        # Set appropriate speeds
        if axis.upper() == 'RA':
            await self.mount.set_velocity(self.fast_velocity, self.slow_velocity)
        else:
            await self.mount.set_velocity(self.slow_velocity, self.fast_velocity)

        # Move to extreme position to find limit
        extreme_pos = 15000000 if direction == 'positive' else -15000000

        if axis.upper() == 'RA':
            await self.mount.move_ha(extreme_pos)
        else:
            await self.mount.move_dec(extreme_pos)

        # Wait for motion to stop (should hit limit)
        if await self.wait_for_motion_stop(axis):
            ha_enc, dec_enc = await self.mount.get_encoder_positions()
            limit_pos = ha_enc if axis.upper() == 'RA' else dec_enc
            return limit_pos
        else:
            await self.mount.stop()
            raise RuntimeError(f"Failed to find {axis} {direction} limit - motion timeout")

    async def calibrate(self, progress_callback: Optional[Callable[[CalibrationProgress], None]] = None) -> Dict:
        """
        Run complete calibration routine.

        Args:
            progress_callback: Optional callback function to receive progress updates

        Returns:
            Calibration data dictionary

        Raises:
            RuntimeError: If calibration fails
        """
        try:
            self._status = CalibrationStatus.IN_PROGRESS
            self._error_message = None

            def update_progress():
                if progress_callback:
                    progress_callback(self.get_progress())

            # Initialize
            self._phase = CalibrationPhase.IDLE
            self._progress = 5.0
            self._current_operation = "Initializing"
            update_progress()

            await self.mount.stop()
            await self.mount.set_velocity(60000, 60000)
            await asyncio.sleep(2)

            # Find HA negative limit
            self._phase = CalibrationPhase.FINDING_HA_NEG
            self._progress = 15.0
            update_progress()

            self._calibration_data['limits']['ra_negative'] = await self.find_limit('negative', 'RA')
            await asyncio.sleep(3)

            # Find HA positive limit
            self._phase = CalibrationPhase.FINDING_HA_POS
            self._progress = 35.0
            update_progress()

            self._calibration_data['limits']['ra_positive'] = await self.find_limit('positive', 'RA')
            await asyncio.sleep(3)

            # Find Dec negative limit (nadir)
            self._phase = CalibrationPhase.FINDING_DEC_NEG
            self._progress = 55.0
            update_progress()

            self._calibration_data['limits']['dec_negative'] = await self.find_limit('positive', 'Dec')
            await asyncio.sleep(3)

            # Find Dec positive limit (zenith)
            self._phase = CalibrationPhase.FINDING_DEC_POS
            self._progress = 75.0
            update_progress()

            self._calibration_data['limits']['dec_positive'] = await self.find_limit('negative', 'Dec')

            # Calculate ranges and home positions
            ra_range = (self._calibration_data['limits']['ra_positive'] -
                        self._calibration_data['limits']['ra_negative'])
            dec_range = (self._calibration_data['limits']['dec_positive'] -
                         self._calibration_data['limits']['dec_negative'])

            ra_center = ((self._calibration_data['limits']['ra_positive'] +
                          self._calibration_data['limits']['ra_negative']) // 2)
            dec_center = ((self._calibration_data['limits']['dec_positive'] +
                           self._calibration_data['limits']['dec_negative']) // 2)

            self._calibration_data.update({
                'ranges': {
                    'ra_encoder_range': ra_range,
                    'dec_encoder_range': dec_range
                },
                'home_ra': ra_center,
                'home_dec': dec_center,
            })

            # Mark as calibrated
            self._calibration_data['calibrated'] = True
            self._status = CalibrationStatus.COMPLETED
            self._phase = CalibrationPhase.COMPLETE
            self._progress = 100.0
            self._current_operation = "Calibration complete"
            update_progress()

            return self._calibration_data.copy()

        except Exception as e:
            self._status = CalibrationStatus.FAILED
            self._error_message = str(e)
            self._current_operation = "Calibration failed"
            if progress_callback:
                progress_callback(self.get_progress())
            await self.mount.stop()
            raise RuntimeError(f"Calibration failed: {e}")


    def get_limits_summary(self) -> Dict[str, any]:
        """Get summary of calibration limits and ranges."""
        if not self.is_calibrated:
            return {"calibrated": False}

        limits = self._calibration_data['limits']
        ranges = self._calibration_data['ranges']

        return {
            "calibrated": True,
            "ra_range_steps": ranges['ra_encoder_range'],
            "dec_range_steps": ranges['dec_encoder_range'],
            "ra_limits": {
                "negative_hours": limits['ra_negative'],
                "positive_hours": limits['ra_positive']
            },
            "dec_limits": {
                "negative_nadir": limits['dec_negative'],
                "positive_zenith": limits['dec_positive']
            },
            "home_position": {
                "ra_encoder": self._calibration_data['home_ra'],
                "dec_encoder": self._calibration_data['home_dec']
            },
        }


