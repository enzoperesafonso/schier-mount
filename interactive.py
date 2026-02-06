import asyncio
import sys
import logging
from your_module_name import SchierMount

# Setup basic logging to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)


async def handle_input(mount):
    print("\n--- SchierMount Terminal Controller ---")
    print("Commands: init, home, park, stop, pos, exit")

    while True:
        # Standard input reading in a non-blocking way
        print("Command > ", end='', flush=True)
        line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        cmd = line.strip().lower()

        try:
            if cmd == "init":
                await mount.init_mount()
            elif cmd == "home":
                await mount.home_mount()
            elif cmd == "park":
                await mount.park_mount()
            elif cmd == "stop":
                await mount.stop_mount()
            elif cmd == "pos":
                p = mount.current_positions
                print(f"\n[POS] RA: {p['ra_enc']} | DEC: {p['dec_enc']}")
                print(f"[STATE] {mount.state}\n")
            elif cmd == "exit":
                await mount.stop_mount()
                break
            else:
                print(f"Unknown command: {cmd}")
        except Exception as e:
            print(f"Execution Error: {e}")


async def main():
    mount = SchierMount()
    # The status loop is started inside mount.init_mount() in your class
    # but we run the input handler here.
    await handle_input(mount)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")