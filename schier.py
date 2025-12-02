import asyncio
import logging
from enum import Enum, auto


from comm import MountComm


class MountState(Enum):
    IDLE = auto()
    SLEWING = auto()
    TRACKING = auto()
    PARKING = auto()
    PARKED = auto()
    HOMING = auto()
    FAULT = auto()
    RECOVERING = auto()
    UNKNOWN = auto()


class SchierMount():
    def __init__(self):

        self.comm = MountComm()

        self.offset_ra = 0.0
        self.offset_dec = 0.0

        self.state = MountState.UNKNOWN

        self.encoder_status = {}

        self.logger = logging.getLogger("SchierMount")

        # Very Snazzy and Cool Async Task Stuff
        self._com_lock = asyncio.Lock()
        self._poll_task = None

    async def initialize(self):
        self.logger.debug("Connected to Schier Mount!")

        self._poll_task = asyncio.create_task(self._status_loop())

        # Wait for first status to populate
        while self.encoder_status == {}:
            await asyncio.sleep(0.1)

    async def disconnect(self):
        pass

    async def slew_to_ha_dec(self):
        pass

    async def set_offset_ra_dec(self):
        pass

    async def get_offset_ra_dec(self):
        pass

    async def track_at_rates(self):
        self.state = MountState.TRACKING

    async def stop_tracking(self):
        self.state = MountState.IDLE

    async def home(self):
        pass

    async def park(self):
        pass

    async def unpark(self):
        self.state = MountState.IDLE

    async def stop(self):
        self.state = MountState.IDLE


    def _check_status(self, ra_status, dec_status):
        """
        Analyzes the status bits returned by the mount.
        Returns a list of active faults, or empty list if safe.
        """
        faults = []

        # 1. Check Hardware Flags (E-Stop, Amp Disable)
        # Derived from mountd_main.c error checks
        if ra_status.get('estop') or dec_status.get('estop'):
            faults.append("E-STOP ACTIVE")

        if ra_status.get('amp_disabled') or dec_status.get('amp_disabled'):
            faults.append("AMPLIFIER DISABLED")

        # 2. Check Hardware Limits
        # Derived from mountd_main.c limit checks
        if ra_status.get('pos_limit'): faults.append("RA_POS_LIMIT")
        if ra_status.get('neg_limit'): faults.append("RA_NEG_LIMIT")
        if dec_status.get('pos_limit'): faults.append("DEC_POS_LIMIT")
        if dec_status.get('neg_limit'): faults.append("DEC_NEG_LIMIT")

        return faults

    async def _status_loop(self):
        """
        Polls pos/status and checks safety.

        """
        while True:
            try:
                # 1. Fetch Data (Run in thread to avoid blocking async loop)
                loop = asyncio.get_running_loop()

                # Fetch Status bits (Status2RA/Dec)
                ra_stat = await loop.run_in_executor(None, self.comm.get_axis_status_bits, 0)
                dec_stat = await loop.run_in_executor(None, self.comm.get_axis_status_bits, 1)

                # Fetch Positions (Status1RA/Dec)
                ra_enc, ra_act = await loop.run_in_executor(None, self.comm.get_encoder_position, 0)
                dec_enc, dec_act = await loop.run_in_executor(None, self.comm.get_encoder_position, 1)

                # 2. Safety Check
                faults = self._check_status(ra_stat, dec_stat)

                if faults and self.state != MountState.RECOVERING:
                    self.logger.error(f"SAFETY FAULT DETECTED: {faults}")
                    self.state = MountState.FAULT
                   # await self._emergency_stop()

                    # Trigger auto-recovery logic
                   # asyncio.create_task(self._recovery_procedure(faults))

                # 3. Update State
                self.encoder_status = {
                    'ra_enc': ra_act,
                    'dec_enc': dec_act,
                    'faults': faults,
                }

            except Exception as e:
                self.logger.error(f"Status Loop Error: {e}")

            await asyncio.sleep(0.1)  # 5Hz Polling
