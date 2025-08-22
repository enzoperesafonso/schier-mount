#!/usr/bin/env python3
"""
Test script for the ROTSE-III pyobs telescope module.
Demonstrates integration between the async telescope driver and pyobs.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from pyobs.utils.enums import MotionStatus
from pyobs.utils.time import Time
from astropy.coordinates import SkyCoord, EarthLocation
import astropy.units as u

# Import our ROTSE-III telescope module
from rotse3_telescope import ROTSE3Telescope

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

log = logging.getLogger(__name__)


async def test_rotse3_pyobs_module():
    """Test the ROTSE-III pyobs telescope module."""
    
    print("🔭 ROTSE-III pyobs Module Test")
    print("=" * 50)
    
    # Create telescope configuration with embedded driver config
    telescope_config = {
        'slew_timeout': 300.0,
        'position_tolerance': 0.01,
        
        # Site configuration (HESS site)
        'timezone': -7,
        'location': {
            'longitude': -111.6,    # degrees E
            'latitude': -23.27,     # degrees N  
            'elevation': 1800       # meters
        },
        'min_altitude': 10.0,
        
        # FITS headers
        'fits_headers': {
            'TELESCOP': ['ROTSE-III', 'Telescope name'],
            'INSTRUME': ['ROTSE3-HESS', 'Instrument name'],
            'SITE': ['HESS', 'Observatory site']
        },
        
        # Embedded telescope driver configuration
        'telescope': {
            # Serial communication settings
            'serial': {
                'port': '/dev/ttyS0',
                'baudrate': 9600,
                'timeout': 2.0
            },
            
            # Encoder limits and ranges
            'limits': {
                'ha_positive': 262143,
                'ha_negative': -262144,
                'dec_positive': 131071,
                'dec_negative': -131072
            },
            
            'ranges': {
                'ha_encoder_range': 24.0,
                'dec_encoder_range': 180.0
            },
            
            # Coordinate system and site parameters
            'coordinates': {
                'longitude': -111.6,
                'latitude': -23.27,
                'elevation': 1800.0,
                'timezone': -7
            },
            
            # Motion parameters for different slew modes
            'motion': {
                'normal_speed': 600,
                'precise_speed': 100,
                'fast_speed': 1200
            },
            
            # Tracking parameters
            'tracking': {
                'sidereal_rate': 15.041,
                'update_interval': 1.0
            },
            
            # Safety parameters and limits
            'safety': {
                'max_slew_time': 300.0,
                'position_tolerance': 0.01,
                'min_altitude': 10.0
            },
            
            # Status monitoring settings
            'monitoring': {
                'status_interval': 2.0,
                'encoder_tolerance': 10
            }
        }
    }
    
    # Create telescope instance
    telescope = ROTSE3Telescope(**telescope_config)
    
    # Set observer location
    location = EarthLocation(
        lon=telescope_config['location']['longitude'] * u.deg,
        lat=telescope_config['location']['latitude'] * u.deg,
        height=telescope_config['location']['elevation'] * u.m
    )
    telescope.observer = location
    
    try:
        # Test 1: Open telescope connection
        print("\n1️⃣  Opening telescope connection...")
        await telescope.open()
        print("✅ Telescope connection opened")
        
        # Test 2: Check ready status
        print("\n2️⃣  Checking telescope ready status...")
        is_ready = await telescope.is_ready()
        print(f"   Ready status: {'✅ Ready' if is_ready else '❌ Not ready'}")
        
        # Test 3: Get motion status
        print("\n3️⃣  Getting motion status...")
        motion_status = await telescope.get_motion_status()
        print(f"   Motion status: {motion_status.value}")
        
        # Test 4: Initialize telescope (if not already initialized)
        if not is_ready and motion_status != MotionStatus.PARKED:
            print("\n4️⃣  Initializing telescope...")
            response = input("   Initialize telescope? This will move the telescope! [y/N]: ")
            if response.lower() in ['y', 'yes']:
                try:
                    await telescope.init()
                    print("   ✅ Telescope initialized successfully")
                except Exception as e:
                    print(f"   ❌ Initialization failed: {e}")
            else:
                print("   ⏭️  Skipping initialization")
        else:
            print("\n4️⃣  Telescope already initialized")
        
        # Test 5: Get current position
        print("\n5️⃣  Getting current position...")
        try:
            ra, dec = await telescope.get_radec()
            print(f"   Current position: RA={ra:.5f}°, Dec={dec:.5f}° (J2000)")
            
            # Convert to sexagesimal
            coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
            ra_str = coord.ra.to_string(sep=':', unit=u.hour, pad=True)
            dec_str = coord.dec.to_string(sep=':', unit=u.deg, pad=True)
            print(f"   Sexagesimal: RA={ra_str}, Dec={dec_str}")
            
        except Exception as e:
            print(f"   ❌ Failed to get position: {e}")
        
        # Test 6: Test offsets
        print("\n6️⃣  Testing RA/Dec offsets...")
        try:
            # Get current offsets
            ra_offset, dec_offset = await telescope.get_offsets_radec()
            print(f"   Current offsets: dRA={ra_offset:.5f}°, dDec={dec_offset:.5f}°")
            
            # Set small test offsets
            test_ra_offset = 0.001  # 3.6 arcseconds
            test_dec_offset = 0.001  # 3.6 arcseconds
            
            print(f"   Setting test offsets: dRA={test_ra_offset:.5f}°, dDec={test_dec_offset:.5f}°")
            await telescope.set_offsets_radec(test_ra_offset, test_dec_offset)
            
            # Verify offsets were set
            new_ra_offset, new_dec_offset = await telescope.get_offsets_radec()
            print(f"   New offsets: dRA={new_ra_offset:.5f}°, dDec={new_dec_offset:.5f}°")
            
            # Clear offsets
            await telescope.set_offsets_radec(0.0, 0.0)
            print("   ✅ Offsets cleared")
            
        except Exception as e:
            print(f"   ❌ Offset test failed: {e}")
        
        # Test 7: Test slewing (if initialized)
        is_ready = await telescope.is_ready()
        if is_ready:
            print("\n7️⃣  Testing telescope slewing...")
            response = input("   Perform test slew? This will move the telescope! [y/N]: ")
            if response.lower() in ['y', 'yes']:
                try:
                    # Get current position
                    current_ra, current_dec = await telescope.get_radec()
                    
                    # Calculate a nearby target (1 degree east)
                    target_ra = current_ra + 1.0
                    target_dec = current_dec
                    
                    print(f"   Slewing to RA={target_ra:.5f}°, Dec={target_dec:.5f}°")
                    await telescope.move_radec(target_ra, target_dec)
                    print("   ✅ Slew completed successfully")
                    
                    # Return to original position
                    print("   Returning to original position...")
                    await telescope.move_radec(current_ra, current_dec)
                    print("   ✅ Returned to original position")
                    
                except Exception as e:
                    print(f"   ❌ Slew test failed: {e}")
            else:
                print("   ⏭️  Skipping slew test")
        else:
            print("\n7️⃣  Skipping slew test (telescope not ready)")
        
        # Test 8: Get FITS headers
        print("\n8️⃣  Getting FITS headers...")
        try:
            fits_headers = await telescope.get_fits_header_before()
            
            print("   Key FITS headers:")
            important_keys = ['TELESCOP', 'TEL-RA', 'TEL-DEC', 'TEL-STAT', 'PIERSIDE', 'TRACKING']
            for key in important_keys:
                if key in fits_headers:
                    value, comment = fits_headers[key]
                    print(f"   {key:10s} = {str(value):15s} / {comment}")
            
            print(f"   Total headers: {len(fits_headers)}")
            
        except Exception as e:
            print(f"   ❌ FITS header test failed: {e}")
        
        # Test 9: Test parking (optional)
        print("\n9️⃣  Testing telescope parking...")
        response = input("   Park telescope? This will move it to park position! [y/N]: ")
        if response.lower() in ['y', 'yes']:
            try:
                await telescope.park()
                print("   ✅ Telescope parked successfully")
                
                # Check motion status
                motion_status = await telescope.get_motion_status()
                print(f"   Motion status: {motion_status.value}")
                
            except Exception as e:
                print(f"   ❌ Parking failed: {e}")
        else:
            print("   ⏭️  Skipping parking test")
        
        print("\n✅ All tests completed!")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        log.error(f"Test error: {e}")
        
    finally:
        # Cleanup
        print("\n🧹 Cleaning up...")
        await telescope.close()
        print("✅ Telescope connection closed")
        print("👋 Test completed!")


if __name__ == "__main__":
    try:
        asyncio.run(test_rotse3_pyobs_module())
    except KeyboardInterrupt:
        print("\n⏹️  Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)