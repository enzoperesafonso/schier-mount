from utils.calibration import Calibration
from utils.comm import Comm
import asyncio

comm = Comm()

cal = Calibration(comm)


asyncio.run(cal.calibrate())

