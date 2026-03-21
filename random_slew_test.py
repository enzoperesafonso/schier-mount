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
    
    # Mechanical HA range: [-92.5, 92.5]
    # Mechanical Dec range: [-210, 30] (effectively limited by horizon)
    
    csv_header = [
        "target_ha", "target_dec", "start_time", "end_time", 
        "slew_duration", "status", "error_msg", "final_ha", "final_dec"
    ]
    
    # Open CSV and write header if new
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
            # Generate random HA/Dec within range
            target_ha = random.uniform(-85.0, 85.0)
            target_dec = random.uniform(-90.0, 10.0)
            
            # Check horizon
            if not is_above_horizon(target_ha, target_dec, lat):
                continue
                
            logging.info(f"Target {pointings_completed + 1}/{n_points}: HA={target_ha:.2f}, Dec={target_dec:.2f}")
            
            start_ts = time.time()
            start_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_ts))
            status = "SUCCESS"
            error_msg = ""
            slew_duration = 0.0
            
            try:
                await mount.slew_mount(target_ha, target_dec)
                end_ts = time.time()
                slew_duration = end_ts - start_ts
                logging.info(f"Slew complete in {slew_duration:.2f}s")
            except Exception as e:
                status = "FAILED"
                error_msg = str(e)
                end_ts = time.time()
                slew_duration = end_ts - start_ts
                logging.error(f"Slew failed: {e}")
            
            # Get final coordinates
            final_ha, final_dec = await mount.get_ha_dec()
            end_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_ts))
            
            # Log to CSV
            with open(output_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    f"{target_ha:.4f}", f"{target_dec:.4f}", start_time_str, end_time_str,
                    f"{slew_duration:.2f}", status, error_msg, f"{final_ha:.4f}", f"{final_dec:.4f}"
                ])
            
            pointings_completed += 1
            await asyncio.sleep(1.0) # Small pause between pointings

    except Exception as e:
        logging.critical(f"Critical error in test sequence: {e}")
    finally:
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
