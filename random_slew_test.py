import asyncio
import random
import time
import csv
import logging
import math
import argparse
from schier import SchierMount

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("random_slew_test.log"),
        logging.StreamHandler()
    ]
)

def is_above_horizon(ha, dec, lat_deg, alt_limit=5.0):
    """Checks if HA and Dec are above the horizon for the given latitude."""
    phi = math.radians(lat_deg)
    delta = math.radians(dec)
    h = math.radians(ha)
    
    sin_alt = (math.sin(delta) * math.sin(phi)) + (math.cos(delta) * math.cos(phi) * math.cos(h))
    alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))
    
    return alt >= alt_limit

async def run_random_slews(n_points, output_csv="slew_results.csv"):
    mount = SchierMount()
    config = mount.config
    lat = config.location['latitude']
    
    csv_header = [
        "start_ra", "start_dec", "target_ra", "target_dec", "start_time", "end_time", 
        "slew_duration", "status", "error_msg", "final_ra", "final_dec"
    ]
    
    # Open CSV and write header
    with open(output_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(csv_header)
        
    try:
        logging.info("Initializing mount for random slew test...")
        await mount.init_mount()
        
        # Ensure we are not parked or in a fault state
        if mount.state.name == "PARKED" or mount.state.name == "UNKNOWN":
            logging.info("Mount is parked/unknown, homing...")
            await mount.home_mount()
        
        pointings_completed = 0
        while pointings_completed < n_points:
            # Get starting coordinates
            start_ra, start_dec = await mount.get_ra_dec()

            # Generate random RA/Dec within range
            target_ra = random.uniform(0.0, 360.0)
            target_dec = random.uniform(-90.0, 30.0)
            
            # Convert target RA to HA to check horizon (optional but good for consistency)
            target_ha = mount.coord.ra_to_ha(target_ra)

            # Check horizon
            if not is_above_horizon(target_ha, target_dec, lat):
                continue
                
            logging.info(f"Target {pointings_completed + 1}/{n_points}: RA={target_ra:.2f} (HA={target_ha:.2f}), Dec={target_dec:.2f}")
            
            start_ts = time.time()
            start_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_ts))
            status = "SUCCESS"
            error_msg = ""
            slew_duration = 0.0
            
            try:
                await mount.slew_mount_ra_dec(target_ra, target_dec)
                end_ts = time.time()
                slew_duration = end_ts - start_ts
                logging.info(f"Slew complete in {slew_duration:.2f}s")
            except Exception as e:
                status = "FAILED"
                error_msg = str(e)
                end_ts = time.time()
                slew_duration = end_ts - start_ts
                logging.error(f"Slew failed: {e}")
                
                # Check if we need to recover
                if mount.state.name == "FAULT" or "FAULT" in error_msg:
                    logging.warning("Fault detected! Attempting recovery (re-homing)...")
                    try:
                        break  # Break the while loop to stop tests
                    except Exception as recovery_error:
                        logging.critical(f"RECOVERY FAILED: {recovery_error}. Aborting test sequence.")

                        break # Break the while loop to stop tests
            
            # Get final coordinates
            final_ra, final_dec = await mount.get_ra_dec()
            end_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_ts))
            
            # Log to CSV
            with open(output_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    f"{start_ra:.4f}", f"{start_dec:.4f}",
                    f"{target_ra:.4f}", f"{target_dec:.4f}", start_time_str, end_time_str,
                    f"{slew_duration:.2f}", status, error_msg, f"{final_ra:.4f}", f"{final_dec:.4f}"
                ])
            
            pointings_completed += 1
            await asyncio.sleep(1.0) # Small pause between pointings

    except Exception as e:
        logging.critical(f"Critical error in test sequence: {e}")
    finally:
        try:
            # Only attempt to park if we are not in a hard fault state
            if mount.state.name != "FAULT":
                logging.info("Parking mount...")
                await mount.park_mount()
        except Exception as e:
            logging.error(f"Failed to park mount at end of test: {e}")
            
        await mount.stop_mount()
        logging.info("Test sequence finished.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run N random pointing slews.")
    parser.add_argument("n", type=int, help="Number of random pointings")
    parser.add_argument("--output", type=str, default="slew_results.csv", help="Output CSV filename")
    
    args = parser.parse_args()
    
    try:
        asyncio.run(run_random_slews(args.n, args.output))
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")
