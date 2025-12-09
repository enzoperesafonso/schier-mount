import asyncio
import logging
import sys

# Import your modules
from schier import SchierMount, MountState

# Setup logging to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


async def monitor_state(driver, state, message):
    """
    Background task to print status while a specific state is active.
    """
    print(f"\n--- MONITORING {message.upper()} ---")
    while driver.state == state:
        stat = driver.encoder_status
        enc_ra = stat.get('ra_enc', 'N/A')
        enc_dec = stat.get('dec_enc', 'N/A')

        # Overwrite the same line for a clean display
        sys.stdout.write(f"\r{message} in progress... RA: {enc_ra} | Dec: {enc_dec}   ")
        sys.stdout.flush()
        await asyncio.sleep(0.5)
    print(f"\n--- {message.upper()} MONITOR END ---")


async def main():
    print("=== SCHIER MOUNT FULL CYCLE TEST ===")

    try:
        driver = SchierMount()
    except Exception as e:
        print(f"Initialization Failed: {e}")
        return

    # Start Driver Loop
    driver_task = asyncio.create_task(driver.initialize())

    # Wait for first status
    print("Waiting for driver connection...")
    while not driver.encoder_status:
        await asyncio.sleep(0.1)

    print(f"Initial State: {driver.state}")

    try:
        # 1. Homing
        print("\n>>> STARTING HOMING SEQUENCE <<<")
        print("WARNING: Mount will move to limit switches!")
        monitor_task = asyncio.create_task(monitor_state(driver, MountState.HOMING, "Homing"))
        await driver.home()
        await monitor_task
        print(">>> HOMING COMPLETE <<<")
        print(f"State after homing: {driver.state}")

        # 2. Parking
        print("\n>>> STARTING PARKING SEQUENCE <<<")
        monitor_task = asyncio.create_task(monitor_state(driver, MountState.PARKING, "Parking"))
        await driver.park()
        await monitor_task
        print(">>> PARKING COMPLETE <<<")
        print(f"State after parking: {driver.state}")

        # 3. Unparking
        print("\n>>> STARTING UNPARKING SEQUENCE <<<")
        monitor_task = asyncio.create_task(monitor_state(driver, MountState.PARKED, "Unparking"))
        await driver.unpark()
        await monitor_task
        print(">>> UNPARKING COMPLETE <<<")
        print(f"Final State: {driver.state}")

    except Exception as e:
        print(f"\n!!! TEST FAILED: {e} !!!")

    finally:
        print("\nShutting down...")
        driver_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
