import asyncio
import logging
import aioconsole  # pip install aioconsole
from your_module_name import SchierMount, MountState

# Configure logging to see what the driver is doing
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

async def main_loop():
    mount = SchierMount()
    print("--- SchierMount CLI Test Tool ---")
    print("Commands: init, home, park, standby, stop, status, exit")

    while True:
        # Get user input asynchronously
        cmd = await aioconsole.ainput("Mount > ")
        cmd = cmd.strip().lower()

        try:
            if cmd == "init":
                await mount.init_mount()
            elif cmd == "home":
                await mount.home_mount()
            elif cmd == "park":
                await mount.park_mount()
            elif cmd == "standby":
                await mount.standby_mount()
            elif cmd == "stop":
                await mount.stop_mount()
            elif cmd == "status":
                print(f"State: {mount.state}")
                print(f"Positions: {mount.current_positions}")
            elif cmd == "exit":
                await mount.stop_mount()
                break
            elif cmd == "":
                continue
            else:
                print(f"Unknown command: {cmd}")
        except Exception as e:
            print(f"Error executing command: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass