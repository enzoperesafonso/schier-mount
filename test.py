import asyncio
import logging
import sys

# Import your modules
from comm import MountComm
from schier import SchierMount
from configuration import MountConfig

# Setup logging to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


async def monitor_homing(driver):
    """
    Background task to print status while homing is in progress.
    """
    print("\n--- MONITORING STATUS ---")
    while driver.state == driver.state.HOMING:
        stat = driver.encoder_status
        enc_ra = stat.get('ra_enc', 'N/A')
        enc_dec = stat.get('dec_enc', 'N/A')

        # Overwrite the same line for a clean display
        sys.stdout.write(f"\rHoming in progress... RA: {enc_ra} | Dec: {enc_dec}   ")
        sys.stdout.flush()
        await asyncio.sleep(0.5)
    print("\n--- STATUS MONITOR END ---")


async def main():
    print("=== SCHIER MOUNT HOMING TEST ===")



    try:
        driver = SchierMount()

    except Exception as e:
        print(f"Initialization Failed: {e}")
        return

    # 2. Start Driver Loop
    driver_task = asyncio.create_task(driver.initialize())

    # Wait for first status
    print("Waiting for driver connection...")
    while not driver.encoder_status:
        await asyncio.sleep(0.1)

    print(f"Initial State: {driver.state}")

    # 3. Execute Homing
    try:
        print("\n>>> STARTING HOMING SEQUENCE <<<")
        print("WARNING: Mount will move to limit switches!")

        # Start the monitor to watch the numbers change
        monitor_task = asyncio.create_task(monitor_homing(driver))

        # Triggers: Stop -> Set Homing Vel -> Home Cmd -> Wait -> Sync
        await driver.home()

        await monitor_task

        await driver.park()

        await driver.unpark()

        print("\n>>> HOMING COMPLETE <<<")
        print(f"Final State: {driver.state}")



    except Exception as e:
        print(f"\n!!! HOMING FAILED: {e} !!!")

    finally:
        print("\nShutting down...")
        driver_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass