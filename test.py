import asyncio
import logging
import os
from schier import SchierMount, MountState

# Set up basic logging to see what's happening under the hood
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


async def print_status_forever(mount):
    """Periodically clears the screen and prints the current telemetry."""
    print("Starting Live Telemetry Monitor (Ctrl+C to stop)...")
    await asyncio.sleep(2)  # Give the status loop a moment to start

    try:
        while True:
            # Clear terminal screen (works on Linux/Mac)
            os.system('clear')

            t = mount.telemetry
            print("=" * 40)
            print(f" ROTSE-IIIc MOUNT STATUS | State: {mount.state.name}")
            print("=" * 40)

            print(f"RA  (Actual):  {t['ra_enc']:>10} | {t['ra_deg']:>8.4f}°")
            print(f"RA  (Target):  {t['ra_target_enc']:>10}")
            print("-" * 40)
            print(f"DEC (Actual):  {t['dec_enc']:>10} | {t['dec_deg']:>8.4f}°")
            print(f"DEC (Target):  {t['dec_target_enc']:>10}")
            print("-" * 40)
            print(f"Moving:        {'YES' if t['is_moving'] else 'NO'}")

            # Add a small note if it's in a fault state
            if mount.state == MountState.FAULT:
                print("\n[!] WARNING: Mount is in a FAULT state. Check hardware.")

            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass


async def main():
    # 1. Instantiate the High-Level Driver
    mount = SchierMount()

    try:
        # 2. Initialize (this starts the comms and the _status_loop)
        # Note: Ensure your /dev/ttyS0 permissions are correct!
        await mount.init_mount()

        # 3. Run the monitor
        await print_status_forever(mount)

    except Exception as e:
        print(f"Failed to start test: {e}")
    finally:
        # Ensure we close the serial port on exit
        if hasattr(mount.comm, 'serial'):
            mount.comm.serial.close()
            print("\nSerial connection closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass