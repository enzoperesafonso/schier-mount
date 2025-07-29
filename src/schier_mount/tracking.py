from state import MountStatus, TrackingMode

class Tracking:
    def __init__(self, state: MountStatus):
        self.state = state

        self.running = False
        self.task = None

    async def track_sidereal(self):
        self.state.tracking_mode = TrackingMode.SIDEREAL

    async def track_non_sidereal(self, ha_rate, dec_rate):
        self.state.tracking_mode = TrackingMode.NON_SIDEREAL

    async def stop_track(self):
        self.state.tracking_mode = TrackingMode.STOPPED

