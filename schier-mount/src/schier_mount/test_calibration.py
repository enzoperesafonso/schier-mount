from utils.comm import Comm
from utils.calibration import Calibration


serial = Comm()


calibrator = Calibration(serial)

def progress_update(p):
    print(f"[{p.status.value}] {p.phase.name} — {p.progress_percent:.1f}% — {p.current_operation}")

calibrator.calibrate(progress_callback=progress_update)
print(calibrator.get_limits_summary())
