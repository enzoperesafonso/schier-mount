import asyncio
import logging
from enum import Enum, auto


from comm import MountComm
from configuration import MountConfig


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

        # Start the config file
        self.config = MountConfig()

        # Very Snazzy and Cool Async Task Stuff
        self._com_lock = asyncio.Lock()
        self._poll_task = None

    async def initialize(self):

        self._poll_task = asyncio.create_task(self._status_loop())

        # Wait for first status to populate
        while self.encoder_status == {}:
            await asyncio.sleep(0.1)

        self.logger.info("Connected to Schier Mount!")

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


    async def park(self):
        pass

    async def unpark(self):
        self.state = MountState.IDLE

    async def stop(self):
        self.state = MountState.IDLE

    async def home(self):
        """
        Performs the full homing sequence:
        1. Cancels tracking/slewing.
        2. Sends hardware home command.
        3. Waits for movement to physically stop (encoders settle).
        4. Syncs software coordinates to the 'Stow' position.
        """
        self.logger.info("Starting Homing Sequence...")

        self.state = MountState.HOMING

        try:
            # 2. Send the mount off on an adventure (Thread-safe)
            # We use the lock to ensure no status polls interrupt the sequence
            async with self._com_lock:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.comm.send_home)

            # 3. Wait for completion
            # The mount will move to the limit switch and stop automatically.
            # We monitor the encoders to see when they stop changing.
            self.logger.info("Waiting for mount to find index (this may take time so grab some tea I guess)...")
            await self._wait_for_stop(timeout=180)  # 3 minutes max (homing is slow)

            # 4. Sync Coordinates
            # Once stopped at the physical index, we tell the software:
            # "We are now at the position defined in config."
            # self.update_home_position()

            self.config.encoder['zeropt_ra'] = self.encoder_status['ra_enc']
            self.config.encoder['zeropt_dec'] = self.encoder_status['dec_enc']
            self.state = MountState.IDLE
            self.logger.info(f'Homing Complete. Mount is synced ra: {self.config.encoder['zeropt_ra']} dec: {self.config.encoder['zeropt_ra']} and IDLE.')

        except Exception as e:
            self.logger.error(f"Homing Failed: {e}")
            self.state = MountState.FAULT
            await self.stop()

    async def _wait_for_stop(self, timeout=60):
        """
        Monitors encoders. Returns only when they have been stable
        (not changing) for a specific duration.
        """
        start_time = asyncio.get_running_loop().time()
        last_ra = 0
        last_dec = 0
        stable_count = 0
        required_stable_polls = 50  # 10 seconds at 5Hz polling

        while True:
            # Timeout Check
            if asyncio.get_running_loop().time() - start_time > timeout:
                raise TimeoutError("Homing timed out - Mount did not stop.")

            # Get latest position from the status loop
            # Note: We rely on _status_loop running in the background to update this
            curr_ra = self.encoder_status.get('ra_enc', 0)
            curr_dec = self.encoder_status.get('dec_enc', 0)

            # Check delta (allow tiny jitter of 50 steps)
            delta_ra = abs(curr_ra - last_ra)
            delta_dec = abs(curr_dec - last_dec)

            if delta_ra <= 50 and delta_dec <= 50:
                stable_count += 1
            else:
                stable_count = 0  # Reset count if we detect motion

            if stable_count >= required_stable_polls:
                return  # Motion has stopped

            last_ra = curr_ra
            last_dec = curr_dec

            await asyncio.sleep(0.2)  # 5 Hz same as poll update!

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
                # --- Acquire Lock for Serial I/O ---
                async with self._com_lock:
                    loop = asyncio.get_running_loop()

                    # Run all comms in one block to keep data consistent
                    ra_stat = await loop.run_in_executor(None, self.comm.get_axis_status_bits, 0)
                    dec_stat = await loop.run_in_executor(None, self.comm.get_axis_status_bits, 1)
                    ra_enc, ra_act = await loop.run_in_executor(None, self.comm.get_encoder_position, 0)
                    dec_enc, dec_act = await loop.run_in_executor(None, self.comm.get_encoder_position, 1)
                # 2. Safety Check
                faults = self._check_status(ra_stat, dec_stat)

                if faults and self.state != MountState.RECOVERING: # if we end up here we are cooked
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

            await asyncio.sleep(0.2)  # 5Hz Polling
