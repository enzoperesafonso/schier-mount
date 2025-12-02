import asyncio
import logging
import sys

# Import your modules
from comm import MountComm, MountError
from schier import SchierMount

# Setup nice logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


async def main():
    print("=== Schier Driver Status Loop Test ===")

    # 3. Initialize the Driver
    driver = SchierMount()

    # 4. Start the Driver Background Task
    # We wrap it in a task so it runs concurrently with our print loop below
    print("Starting Driver Loop...")
    driver_task = asyncio.create_task(driver.initialize())

    try:
        # 5. Monitor the Status
        print("\nMonitoring Status (Press Ctrl+C to stop)...\n")

        for i in range(1, 21):  # Run for 20 seconds
            await asyncio.sleep(1.0)

            # Fetch the latest status dictionary from the driver
            status = driver.encoder_status

            if not status:
                print(f"[{i}s] Waiting for first poll...")
                continue

            # Extract key data
            enc_ra = status.get('ra_enc', 'N/A')
            enc_dec = status.get('dec_enc', 'N/A')
            faults = status.get('faults', [])
            timestamp = status.get('timestamp', 0)

            # Print formatted output
            status_line = (
                f"[{i}s] T={timestamp:.2f} | "
                f"EncRA: {enc_ra:>8} | "
                f"EncDec: {enc_dec:>8} | "
                f"Faults: {faults}"
            )

            # Simple visual alert if faults exist
            if faults:
                print(f"!!! FAULT DETECTED: {status_line} !!!")
            else:
                print(status_line)

    except KeyboardInterrupt:
        print("\nTest interrupted by user.")

    finally:
        # 6. Clean Shutdown
        print("Shutting down...")

        # Cancel the driver loop
        driver_task.cancel()
        try:
            await driver_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    # Python 3.7+ entry point
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass