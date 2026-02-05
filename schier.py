import asyncio
import logging
from enum import Enum, auto

import comm

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
        self.logger = logging.getLogger("SchierMount")
        self.comm = comm.MountComm()
        self.state = MountState.UNKNOWN
        self._status_task = None
        self.serial_lock = asyncio.Lock()

    def init_mount(self):

        self.comm.init_mount()

        self.state = MountState.PARKED

    def calibrate_mount(self):
        pass

    def stop_mount(self):
        pass

    def park_mount(self):
        pass

    def unpark_mount(self):
        pass

    def standby_mount(self):
        pass

    def track_sidereal(self):
        pass

    def _check_encoder_stop(self):
        pass

    def _check_mount_at_position(self):
        pass

    async def _status_loop(self):

