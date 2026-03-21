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

def ha_to_hms(ha):
    h_total = ha / 15.0
    h = int(h_total)
    m_total = abs(h_total - h) * 60
    m = int(m_total)
    s = (m_total - m) * 60
    sign = '' if h_total >= 0 else '-'
    return f"{sign}{abs(h):02d}h{m:02d}m{s:04.1f}s"


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
                ha, dec = await mount.get_ha_dec()
                print(f"\n[POS] RA Enc: {p['ra_enc']} | DEC Enc: {p['dec_enc']}")
                print(f"[POS] HA: {ha:.4f} ({ha_to_hms(ha)}) | DEC: {dec:.4f} ({dec_to_dms(dec)})")
                print(f"[STATE] {mount.state}\n")
            elif cmd == "slew":
                if len(args) == 2:
                    ha_deg, dec_deg = float(args[0]), float(args[1])
                    await mount.slew_mount(ha_deg, dec_deg)
                else:
                    print("Usage: slew <ha_deg> <dec_deg>")
            elif cmd == "track":
                await mount.track_sidereal()
            elif cmd == "shift":
                if len(args) == 2:
                    delta_ha, delta_dec = float(args[0]), float(args[1])
                    await mount.shift_mount(delta_ha, delta_dec)
                else:
                    print("Usage: shift <delta_ha> <delta_dec>")
            elif cmd == "track_rate":
                if len(args) == 2:
                    ha_rate, dec_rate = float(args[0]), float(args[1])
                    await mount.track_non_sidereal(ha_rate, dec_rate)
                else:
                    print("Usage: track_rate <ha_rate> <dec_rate>")
            elif cmd == "offset":
                if len(args) == 2:
                    ha_offset, dec_offset = float(args[0]), float(args[1])
                    await mount.update_offsets(ha_offset, dec_offset)
                else:
                    print("Usage: offset <ha_offset> <dec_offset>")
            elif cmd == "get_offsets":
                ha_offset, dec_offset = await mount.get_offsets()
                print(f"HA Offset: {ha_offset}, Dec Offset: {dec_offset}")
            elif cmd == "get_coords":
                ha, dec = await mount.get_ha_dec()
                print(f"HA: {ha:.4f} ({ha_to_hms(ha)})")
                print(f"Dec: {dec:.4f} ({dec_to_dms(dec)})")
            elif cmd == "help":
                print("\n--- SchierMount Terminal Controller ---")
                print("Commands:")
                print("  init          - Initializes the mount hardware.")
                print("  home          - Homes the mount.")
                print("  park          - Parks the mount.")
                print("  zenith        - Moves the mount to the zenith position.")
                print("  stop          - Stops all mount movement.")
                print("  pos           - Shows the current encoder and HA/Dec positions and state.")
                print("  slew <ha> <dec> - Slews the mount to the given HA and Dec.")
                print("  track         - Starts sidereal tracking.")
                print("  shift <dha> <ddec> - Shifts the mount by a relative amount.")
                print("  track_rate <har> <decr> - Starts tracking at a custom rate.")
                print("  offset <hao> <deco> - Sets the HA and Dec offsets.")
                print("  get_offsets   - Gets the current HA and Dec offsets.")
                print("  get_coords    - Gets the current HA and Dec.")
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