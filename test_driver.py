#!/usr/bin/env python3
"""
Test script for the telescope driver system.
Validates the modular components and basic functionality.
"""

import logging
import time
import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

def test_imports():
    """Test that all modules can be imported"""
    logger.info("Testing module imports...")
    
    try:
        from config import TelescopeConfig
        logger.info("✓ TelescopeConfig imported successfully")
        
        from state import MountStatus, MountState, TrackingMode, PierSide, AxisStatus
        logger.info("✓ State classes imported successfully")
        
        from coordinates import Coordinates
        logger.info("✓ Coordinates imported successfully")
        
        from communication import TelescopeCommunication, calculate_crc16
        logger.info("✓ Communication classes imported successfully")
        
        from telescope_driver import TelescopeDriver, SlewMode
        logger.info("✓ TelescopeDriver imported successfully")
        
        return True
        
    except ImportError as e:
        logger.error(f"Import failed: {e}")
        return False

def test_config_system():
    """Test configuration system"""
    logger.info("Testing configuration system...")
    
    try:
        from config import TelescopeConfig
        
        # Test with default configuration
        config = TelescopeConfig()
        
        # Test basic configuration access
        serial_config = config.get_serial_config()
        assert 'port' in serial_config
        assert 'baudrate' in serial_config
        logger.info(f"  Serial config: {serial_config['port']} @ {serial_config['baudrate']}")
        
        # Test motion parameters
        normal_params = config.get_motion_params('normal')
        assert 'ha_velocity' in normal_params
        logger.info(f"  Normal motion: HA vel={normal_params['ha_velocity']}")
        
        # Test calibration data
        cal_data = config.get_calibration_data()
        assert 'observer_latitude' in cal_data
        logger.info(f"  Observer latitude: {cal_data['observer_latitude']}°")
        
        # Test YAML config loading
        config_yaml = TelescopeConfig('telescope_config.yaml')
        yaml_serial = config_yaml.get_serial_config()
        logger.info(f"  YAML config loaded: {yaml_serial['port']}")
        
        logger.info("✓ Configuration system working")
        return True
        
    except Exception as e:
        logger.error(f"Configuration test failed: {e}")
        return False

def test_state_management():
    """Test state management system"""
    logger.info("Testing state management...")
    
    try:
        from state import MountStatus, MountState, TrackingMode, PierSide
        
        # Test mount status
        status = MountStatus()
        assert status.state == MountState.DISCONNECTED
        
        # Test state changes
        status.set_state(MountState.IDLE)
        assert status.state == MountState.IDLE
        assert status.previous_state == MountState.DISCONNECTED
        
        # Test coordinates
        status.set_coordinates(1.5, -20.0, PierSide.NORMAL)
        assert status.current_hour_angle == 1.5
        assert status.current_declination == -20.0
        
        # Test axis status
        status.update_axis_from_status1("RA", 1000000, 999950)
        assert status.ra_axis.commanded_position == 1000000
        assert status.ra_axis.encoder_position == 999950
        
        # Test status parsing (simulate ROTSE-III status response)
        status.update_axis_from_status2("RA", "0001")  # Just brake engaged
        assert status.ra_axis.brake_engaged == True
        assert status.ra_axis.amplifier_disabled == False
        
        logger.info("✓ State management working")
        return True
        
    except Exception as e:
        logger.error(f"State management test failed: {e}")
        return False

def test_coordinates():
    """Test coordinate transformation system"""
    logger.info("Testing coordinate transformations...")
    
    try:
        from coordinates import Coordinates
        from state import MountStatus
        from config import TelescopeConfig
        
        # Create test objects
        config = TelescopeConfig()
        status = MountStatus()
        cal_data = config.get_calibration_data()
        coords = Coordinates(status, cal_data)
        
        # Test coordinate transformations
        test_ha = 2.0  # 2 hours east
        test_dec = -20.0  # 20 degrees south
        
        # Convert to encoder positions
        ha_enc, dec_enc, below_pole = coords.ha_dec_to_encoder_positions(test_ha, test_dec)
        logger.info(f"  HA={test_ha}h, Dec={test_dec}° -> HA_enc={ha_enc}, Dec_enc={dec_enc}, below_pole={below_pole}")
        
        # Convert back
        ha_back, dec_back, below_pole_back = coords.encoder_positions_to_ha_dec(ha_enc, dec_enc)
        logger.info(f"  Back conversion: HA={ha_back:.3f}h, Dec={dec_back:.1f}°, below_pole={below_pole_back}")
        
        # Check round-trip accuracy
        ha_error = abs(test_ha - ha_back)
        dec_error = abs(test_dec - dec_back)
        assert ha_error < 0.001, f"HA round-trip error too large: {ha_error:.6f}h"
        assert dec_error < 0.1, f"Dec round-trip error too large: {dec_error:.3f}°"
        assert below_pole == below_pole_back
        
        # Test position reachability
        reachable = coords.is_position_reachable(test_ha, test_dec)
        logger.info(f"  Position reachable: {reachable}")
        
        # Test below-pole mode
        below_pole_ha = 8.0  # 8 hours - should trigger below-pole
        ha_enc_bp, dec_enc_bp, below_pole_bp = coords.ha_dec_to_encoder_positions(below_pole_ha, test_dec)
        assert below_pole_bp == True, "Below-pole mode not detected"
        logger.info(f"  Below-pole test: HA={below_pole_ha}h -> below_pole={below_pole_bp}")
        
        logger.info("✓ Coordinate transformations working")
        return True
        
    except Exception as e:
        logger.error(f"Coordinate test failed: {e}")
        return False

def test_communication():
    """Test communication system (without actual serial connection)"""
    logger.info("Testing communication system...")
    
    try:
        from communication import TelescopeCommunication, calculate_crc16
        
        # Test CRC calculation
        test_data = b"$StopRA"
        crc = calculate_crc16(test_data)
        logger.info(f"  CRC for '{test_data.decode()}': 0x{crc:04X}")
        
        # Test communication object creation (will fail to connect without hardware)
        comm = TelescopeCommunication("/dev/null", 9600, 1.0)
        stats = comm.get_statistics()
        logger.info(f"  Communication stats: {stats['connected']}")
        
        logger.info("✓ Communication system structure working")
        return True
        
    except Exception as e:
        logger.error(f"Communication test failed: {e}")
        return False

def test_driver_creation():
    """Test driver creation and basic functionality"""
    logger.info("Testing driver creation...")
    
    try:
        from telescope_driver import TelescopeDriver, SlewMode
        
        # Create driver with YAML config
        driver = TelescopeDriver('telescope_config.yaml')
        
        # Test basic properties
        assert not driver.is_connected()
        assert not driver.is_initialized()
        
        # Test status retrieval
        status = driver.get_status()
        assert 'state' in status
        assert 'initialized' in status
        logger.info(f"  Driver state: {status['state']}")
        
        # Test position methods (should return None when not initialized)
        ha, dec = driver.get_position()
        assert ha is None and dec is None
        
        logger.info("✓ Driver creation working")
        return True
        
    except Exception as e:
        logger.error(f"Driver creation test failed: {e}")
        return False

def run_all_tests():
    """Run all validation tests"""
    logger.info("=== ROTSE-III Telescope Driver Validation ===")
    
    tests = [
        ("Module Imports", test_imports),
        ("Configuration System", test_config_system),
        ("State Management", test_state_management),
        ("Coordinate Transformations", test_coordinates),
        ("Communication System", test_communication),
        ("Driver Creation", test_driver_creation),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        logger.info(f"\n--- {test_name} ---")
        try:
            if test_func():
                passed += 1
            else:
                logger.error(f"FAILED: {test_name}")
        except Exception as e:
            logger.error(f"FAILED: {test_name} - {e}")
    
    logger.info(f"\n=== Test Results ===")
    logger.info(f"Passed: {passed}/{total}")
    logger.info(f"Failed: {total - passed}/{total}")
    
    if passed == total:
        logger.info("🎉 All tests passed! The telescope driver system is ready for use.")
        return True
    else:
        logger.error("❌ Some tests failed. Please review the errors above.")
        return False

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)