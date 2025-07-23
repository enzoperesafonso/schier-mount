from .utils.calibration import Calibration


cal = Calibration()


cal.calibrate()


cal.save_config_yaml("calibration.yaml")