import asyncio
import logging
from schier import SchierMount

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def run_test_sequence():
    mount = SchierMount()
    try:
        logging.info("Initializing mount...")
        await mount.init_mount()
        logging.info("Mount initialized successfully.")

        logging.info("Homing mount...")
        await mount.home_mount()
        logging.info("Mount homed successfully.")

        logging.info("Sending mount to standby position...")
        await mount.standby_mount()
        logging.info("Mount sent to standby position successfully.")

    except Exception as e:
        logging.error(f"Test sequence failed: {e}")
    finally:
        logging.info("Stopping mount to ensure motors are off...")
        await mount.stop_mount()
        logging.info("Mount stopped.")

if __name__ == "__main__":
    asyncio.run(run_test_sequence())
