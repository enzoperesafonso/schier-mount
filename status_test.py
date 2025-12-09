import asyncio
import logging
import sys
from datetime import datetime

from schier import SchierMount

LOG_FILENAME = "status_log.txt"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


async def status_monitor(driver, log_file):
    """
    Continuously polls and logs all available mount status.
    """
    logger.info(f"Starting continuous status logging to console and '{LOG_FILENAME}'...")
    log_file.write("--- Status Log Start ---
")
    log_file.flush()

    while True:
        try:
            # Use the driver's internal lock to ensure thread-safe serial communication
            async with driver._com_lock:
                loop = asyncio.get_running_loop()

                # Get all status data within one locked block
                ra_stat = await loop.run_in_executor(None, driver.comm.get_axis_status_bits, 0)
                dec_stat = await loop.run_in_executor(None, driver.comm.get_axis_status_bits, 1)
                ra_cmd, ra_act = await loop.run_in_executor(None, driver.comm.get_encoder_position, 0)
                dec_cmd, dec_act = await loop.run_in_executor(None, driver.comm.get_encoder_position, 1)

            # Format the output
            timestamp = datetime.now().isoformat()
            status_string = f"[{timestamp}]\n"
            status_string += f"  State: {driver.state.name}\n"
            status_string += f"  RA Encoder: Actual={ra_act}, Command={ra_cmd}\n"
            status_string += f"  DEC Encoder: Actual={dec_act}, Command={dec_cmd}\n"
            status_string += f"  RA Status: {ra_stat}\n"
            status_string += f"  DEC Status: {dec_stat}\n"
            status_string += "-" * 20 + "\n\n"

            # Print to console
            sys.stdout.write(status_string)
            sys.stdout.flush()

            # Write to file
            log_file.write(status_string)
            log_file.flush()

            await asyncio.sleep(1)

        except Exception as e:
            error_message = f"Error in status monitor: {e}\n"
            sys.stderr.write(error_message)
            log_file.write(error_message)
            log_file.flush()
            await asyncio.sleep(5)  # Wait longer after an error


async def main():
    print("=== SCHIER MOUNT STATUS LOGGER ===")
    log_file = None
    monitor_task = None
    driver_task = None
    driver = None

    try:
        driver = SchierMount()
    except Exception as e:
        print(f"Initialization Failed: {e}")
        return

    try:
        driver_task = asyncio.create_task(driver.initialize())

        print("Waiting for driver connection...")
        while not driver.encoder_status:
            await asyncio.sleep(0.1)
        print(f"Initial State: {driver.state}")

        print("\n>>> STARTING HOMING SEQUENCE <<<")
        await driver.home()
        print(">>> HOMING COMPLETE <<<")

        log_file = open(LOG_FILENAME, "w")
        monitor_task = asyncio.create_task(status_monitor(driver, log_file))

        # Wait indefinitely until the program is interrupted
        await asyncio.Event().wait()

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutdown signal received...")
    finally:
        print("Cleaning up...")
        if monitor_task:
            monitor_task.cancel()
        if driver_task:
            driver_task.cancel()

        # Give tasks a moment to cancel
        await asyncio.sleep(1)

        if log_file:
            log_file.close()
            print(f"Log file '{LOG_FILENAME}' closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # This is handled inside main now, but this catches the final exit
        print("\nProgram stopped by user.")
