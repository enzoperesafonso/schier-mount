from utils.calibration import Calibration
from utils.comm import Comm

comm = Comm()

cal = Calibration(comm)


cal.calibrate()

