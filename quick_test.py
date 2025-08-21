#!/usr/bin/env python3
"""
Quick test script for the ROTSE-III async telescope driver.
Uses /dev/ttyS0 as specified by the user.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from async_telescope_driver import AsyncTelescopeDriver, SlewMode
from state import MountState, TrackingMode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def test_telescope():
    """Quick test of telescope functionality"""
    
    print("🔭 ROTSE-III Telescope Quick Test")
    print("=" * 40)
    
    # Create driver with correct port
    telescope = AsyncTelescopeDriver(
        config_file='telescope_config.yaml',
        port='/dev/ttyS0'  # Use the correct port
    )
    
    try:
        # Step 1: Connect
        print("\n1️⃣  Connecting to telescope...")
        if not await telescope.connect():
            print("❌ Failed to connect")
            return
        print("✅ Connected successfully")
        
        # Step 2: Show initial status
        print("\n2️⃣  Initial status:")
        status = telescope.get_status()
        print(f"   State: {status['state']}")
        print(f"   Connected: {status['connected']}")
        
        # Step 3: Test communication
        print("\n3️⃣  Testing communication...")
        comm_test = await telescope.comm.test_communication()
        print(f"   Result: {'✅ PASS' if comm_test else '❌ FAIL'}")
        
        # Step 4: Get encoder positions
        print("\n4️⃣  Reading encoder positions...")
        await telescope._update_telescope_status()
        ha_enc = telescope.status.ra_axis.encoder_position
        dec_enc = telescope.status.dec_axis.encoder_position
        print(f"   HA Encoder: {ha_enc}")
        print(f"   Dec Encoder: {dec_enc}")
        
        # Step 5: Test coordinate conversion (if we have a coordinate system)
        if telescope.coords:
            print("\n5️⃣  Testing coordinate conversion...")
            if ha_enc is not None and dec_enc is not None:
                try:
                    ha, dec, below_pole = telescope.coords.encoder_positions_to_ha_dec(ha_enc, dec_enc)
                    print(f"   Position: HA={ha:.4f}h, Dec={dec:.3f}°, Below pole={below_pole}")
                except Exception as e:
                    print(f"   Conversion error: {e}")
            else:
                print("   Cannot convert - encoder positions unknown")
        
        # Step 6: Ask about initialization
        print(f"\n6️⃣  Telescope initialized: {telescope.is_initialized()}")
        if not telescope.is_initialized():
            print("   💡 Run initialization with: await telescope.initialize()")
            
            response = input("\n   Initialize telescope now? [y/N]: ")
            if response.lower() in ['y', 'yes']:
                print("   🏠 Starting initialization (this will move the telescope)...")
                result = await telescope.initialize()
                
                if result.success:
                    print(f"   ✅ Initialization completed in {result.duration_seconds:.1f}s")
                    print(f"   🏠 Home positions: HA={result.home_ha_encoder}, Dec={result.home_dec_encoder}")
                else:
                    print(f"   ❌ Initialization failed: {result.message}")
        
        # Step 7: Show final status
        print("\n7️⃣  Final status:")
        status = telescope.get_status()
        print(f"   State: {status['state']}")
        print(f"   Initialized: {status['initialized']}")
        pos = status['position']
        if pos['ha'] is not None and pos['dec'] is not None:
            print(f"   Position: HA={pos['ha']:.4f}h, Dec={pos['dec']:.3f}°")
        else:
            print(f"   Position: Unknown")
        
        print("\n✅ Test completed successfully!")
        
    except KeyboardInterrupt:
        print("\n⏹️  Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        logging.error(f"Test error: {e}")
    finally:
        # Cleanup
        print("\n🔌 Disconnecting...")
        await telescope.disconnect()
        print("👋 Done!")

if __name__ == "__main__":
    try:
        asyncio.run(test_telescope())
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)