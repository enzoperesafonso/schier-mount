import asyncio
import logging
from schier import SchierMount, MountState

# Setup logging to see the "any_error" or "validation" logs from comm.py
logging.basicConfig(level=logging.INFO)


async def run_homing_test():
    mount = SchierMount()

    print("Connecting to mount and starting status loop...")
    await mount.init_mount()

    # Wait a moment for the first few telemetry pulses to populate current_positions
    await asyncio.sleep(1)

    print(f"Current State: {mount.state.name}")
    print(f"Initial Position - RA: {mount.current_positions['ra_enc']}, Dec: {mount.current_positions['dec_enc']}")

    try:
        print("\nSending Home command...")
        mount.state = MountState.HOMING

        # Use safe_comm to send the homing command
        await mount._safe_comm(mount.comm.home_mount)

        print("Homing initiated. Waiting for encoders to stabilize...")

        # This uses your logic: waits until movement is < tolerance for 5 seconds
        await mount._await_encoder_stop(tolerance=5, timeout=120)

        print("\n" + "=" * 30)
        print("SUCCESS: Encoders have stopped.")
        print(f"Final Position - RA: {mount.current_positions['ra_enc']}, Dec: {mount.current_positions['dec_enc']}")
        print("=" * 30)

        mount.state = MountState.IDLE

        await mount._safe_comm(mount.comm.zero_mount)

        await asyncio.sleep(1)

        print("\nSending park command...")
        mount.state = MountState.PARKING

        # Use safe_comm to send the homing command
        await mount._safe_comm(mount.comm.park_mount)

        print("Parking initiated. Waiting for encoders to stabilize...")

        # This uses your logic: waits until movement is < tolerance for 5 seconds
        await mount._await_mount_at_position()

        print("\n" + "=" * 30)
        print("SUCCESS: Encoders have Reached park.")
        print(f"Final Position - RA: {mount.current_positions['ra_enc']}, Dec: {mount.current_positions['dec_enc']}")
        print("=" * 30)

        mount.state = MountState.PARKED

    except TimeoutError as e:
        print(f"\n[!] ERROR: {e}")
    except Exception as e:
        print(f"\n[!] Unexpected Error during homing: {e}")
    finally:
        # Cleanup: Stop the background task and close serial
        if mount._status_task:
            mount._status_task.cancel()
        mount.comm.serial.close()
        print("Serial connection closed.")


if __name__ == "__main__":
    asyncio.run(run_homing_test())