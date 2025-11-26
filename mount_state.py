from enum import Enum

class MountState(Enum):
    """Overall mount states"""
    IDLE = 'idle'
    STOPPING = 'stopping'
    SLEWING = 'slewing'
    ABORTING = 'aborting'
    PARKING = 'parking'
    PARKED = 'parked'
    TRACKING = 'tracking'
    INIT = 'init'
    ERROR = 'error'
    SEVERE_ERROR = 'severe'
