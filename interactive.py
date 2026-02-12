import asyncio
import sys
import logging
from schier import SchierMount

# Setup basic logging to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def dec_to_dms(dec):
    sign = '+' if dec >= 0 else '-'
    dec = abs(dec)
    d = int(dec)
    m_total = (dec - d) * 60
    m = int(m_total)
    s = (m_total - m) * 60
    return f"{sign}{d:02d}°{m:02d}'{s:04.1f}\""

def ra_to_hms(ra):
    h_total = ra / 15.0
    h = int(h_total)
    m_total = (h_total - h) * 60
    m = int(m_total)
    s = (m_total - m) * 60
    return f"{h:02d}h{m:02d}m{s:04.1f}s"


async def handle_input(mount):
    print("\n--- SchierMount Terminal Controller ---")
    print("Commands: init, home, park, stop, pos, exit, slew, track, shift, track_rate, offset, get_offsets, get_coords, help")

    while True:
        # Standard input reading in a non-blocking way
        print("Command > ", end='', flush=True)
        line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        parts = line.strip().lower().split()
        if not parts:
            continue
        cmd = parts[0]
        args = parts[1:]


        try:
            if cmd == "init":
                await mount.init_mount()
            elif cmd == "home":
                await mount.home_mount()
            elif cmd == "park":
                await mount.park_mount()
            elif cmd == "zenith":
                await mount.standby_mount()
            elif cmd == "stop":
                await mount.stop_mount()
            elif cmd == "pos":
                p = mount.current_positions
                ra, dec = await mount.get_ra_dec()
                print(f"\n[POS] RA Enc: {p['ra_enc']} | DEC Enc: {p['dec_enc']}")
                print(f"[POS] RA: {ra:.4f} ({ra_to_hms(ra)}) | DEC: {dec:.4f} ({dec_to_dms(dec)})")
                print(f"[STATE] {mount.state}\n")
            elif cmd == "slew":
                if len(args) == 2:
                    ra_deg, dec_deg = float(args[0]), float(args[1])
                    await mount.slew_mount(ra_deg, dec_deg)
                else:
                    print("Usage: slew <ra_deg> <dec_deg>")
            elif cmd == "track":
                await mount.track_sidereal()
            elif cmd == "shift":
                if len(args) == 2:
                    delta_ra, delta_dec = float(args[0]), float(args[1])
                    await mount.shift_mount(delta_ra, delta_dec)
                else:
                    print("Usage: shift <delta_ra> <delta_dec>")
            elif cmd == "track_rate":
                if len(args) == 2:
                    ra_rate, dec_rate = float(args[0]), float(args[1])
                    await mount.track_non_sidereal(ra_rate, dec_rate)
                else:
                    print("Usage: track_rate <ra_rate> <dec_rate>")
            elif cmd == "offset":
                if len(args) == 2:
                    ra_offset, dec_offset = float(args[0]), float(args[1])
                    await mount.update_offsets(ra_offset, dec_offset)
                else:
                    print("Usage: offset <ra_offset> <dec_offset>")
            elif cmd == "get_offsets":
                ra_offset, dec_offset = await mount.get_offsets()
                print(f"RA Offset: {ra_offset}, Dec Offset: {dec_offset}")
            elif cmd == "get_coords":
                ra, dec = await mount.get_ra_dec()
                print(f"RA: {ra:.4f} ({ra_to_hms(ra)})")
                print(f"Dec: {dec:.4f} ({dec_to_dms(dec)})")
            elif cmd == "help":
                print("\n--- SchierMount Terminal Controller ---")
                print("Commands:")
                print("  init          - Initializes the mount hardware.")
                print("  home          - Homes the mount.")
                print("  park          - Parks the mount.")
                print("  zenith        - Moves the mount to the zenith position.")
                print("  stop          - Stops all mount movement.")
                print("  pos           - Shows the current encoder and RA/Dec positions and state.")
                print("  slew <ra> <dec> - Slews the mount to the given RA and Dec.")
                print("  track         - Starts sidereal tracking.")
                print("  shift <dra> <ddec> - Shifts the mount by a relative amount.")
                print("  track_rate <rar> <decr> - Starts tracking at a custom rate.")
                print("  offset <rao> <deco> - Sets the RA and Dec offsets.")
                print("  get_offsets   - Gets the current RA and Dec offsets.")
                print("  get_coords    - Gets the current RA and Dec.")
                print("  exit          - Stops the mount and exits the program.")

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