import asyncio
import logging
from schier import SchierMount

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def dec_to_dms(dec):
    sign = '+' if dec >= 0 else '-'
    dec = abs(dec)
    d = int(dec)
    m_total = (dec - d) * 60
    m = int(m_total)
    s = (m_total - m) * 60
    return f"{sign}{d:02d}°{m:02d}'{s:04.1f}"

def ra_to_hms(ra):
    h_total = ra / 15.0
    h = int(h_total)
    m_total = (h_total - h) * 60
    m = int(m_total)
    s = (m_total - m) * 60
    return f"{h:02d}h{m:02d}m{s:04.1f}s"

async def monitor_status(mount):
    """Continuously prints mount status."""
    while True:
        p = mount.current_positions
        ra, dec = await mount.get_ra_dec()
        logging.info(f"  [STATUS] State: {mount.state} | RA Enc: {p['ra_enc']} | DEC Enc: {p['dec_enc']} | RA: {ra:.4f} | Dec: {dec:.4f}")
        await asyncio.sleep(1)


async def run_test_sequence():
    mount = SchierMount()
    monitor_task = None
    try:
        logging.info("Initializing mount...")
        await mount.init_mount()
        logging.info("Mount initialized successfully.")
        
        # Start the status monitor
        monitor_task = asyncio.create_task(monitor_status(mount))

        logging.info("Homing mount...")
        await mount.home_mount()
        logging.info("Mount homed successfully.")

        logging.info("Sending mount to standby position...")
        await mount.standby_mount()
        logging.info("Mount sent to standby position successfully.")

    except Exception as e:
        logging.error(f"Test sequence failed: {e}")
    finally:
        if monitor_task:
            monitor_task.cancel()
        logging.info("Stopping mount to ensure motors are off...")
        await mount.stop_mount()
        logging.info("Mount stopped.")

if __name__ == "__main__":
    asyncio.run(run_test_sequence())
