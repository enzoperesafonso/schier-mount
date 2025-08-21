"""
Configuration management for telescope driver.
Handles loading, validation, and management of telescope parameters.
"""

import yaml
import json
from pathlib import Path
from typing import Dict, Any, Optional, Union
import logging

logger = logging.getLogger(__name__)


class TelescopeConfig:
    """
    Telescope configuration management.
    
    Handles loading configuration from YAML/JSON files and provides
    access to telescope parameters with validation.
    """
    
    def __init__(self, config_file: Optional[Union[str, Path]] = None):
        """
        Initialize configuration.
        
        Args:
            config_file: Path to configuration file (YAML or JSON)
        """
        # Default configuration
        self._config = self._get_default_config()
        
        # Load from file if provided
        if config_file:
            self.load_from_file(config_file)
        
        # Validate configuration
        self._validate()
        
        logger.info("Telescope configuration initialized")
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration values"""
        return {
            # Serial communication settings
            'serial': {
                'port': '/dev/ttyS0',
                'baudrate': 9600,
                'timeout': 3.0,
                'retries': 3
            },
            
            # Telescope mechanical limits (encoder positions)
            'limits': {
                'ha_positive': 3447618,     # HA positive limit (home position after homing)
                'dec_negative': -1560846,   # Dec negative limit (home position after homing)
                # ha_negative and dec_positive calculated from ranges
            },
            
            # Encoder ranges (total travel distance)
            'ranges': {
                'ha_encoder_range': 4492409,
                'dec_encoder_range': 4535993
            },
            
            # Coordinate system parameters
            'coordinates': {
                'observer_latitude': -23.2716,  # HESS site latitude
                'dec_steps_per_degree': 19408.0
            },
            
            # Motion parameters for different slew modes
            'motion': {
                'normal': {
                    'ha_velocity': 30000,
                    'dec_velocity': 30000,
                    'ha_acceleration': 3000,
                    'dec_acceleration': 1500
                },
                'fast': {
                    'ha_velocity': 50000,
                    'dec_velocity': 25000,
                    'ha_acceleration': 10000,
                    'dec_acceleration': 5000
                },
                'precise': {
                    'ha_velocity': 5000,
                    'dec_velocity': 3000,
                    'ha_acceleration': 1000,
                    'dec_acceleration': 300
                },
                'initialization': {
                    'ha_velocity': 15000,
                    'dec_velocity': 10000,
                    'ha_acceleration': 2000,
                    'dec_acceleration': 1000
                }
            },
            
            # Tracking parameters
            'tracking': {
                'sidereal_rate_steps_per_sec': 104.0,
                'tracking_safety_margin_steps': 10000  # Stop tracking this many steps before limit
            },
            
            # Safety parameters
            'safety': {
                'slew_timeout_seconds': 300.0,
                'position_tolerance_steps': 5000,
                'initialization_tolerance_steps': 2000,
                'safety_margin_steps': 20000,  # General safety margin from limits
                'max_position_error_steps': 15000
            },
            
            # Monitoring settings
            'monitoring': {
                'status_update_interval': 1.0,
                'position_update_max_age': 5.0
            }
        }
    
    def load_from_file(self, config_file: Union[str, Path]) -> None:
        """
        Load configuration from file.
        
        Args:
            config_file: Path to YAML or JSON configuration file
        """
        config_path = Path(config_file)
        
        if not config_path.exists():
            logger.warning(f"Configuration file not found: {config_path}, using defaults")
            return
        
        try:
            with open(config_path, 'r') as f:
                if config_path.suffix.lower() in ['.yml', '.yaml']:
                    file_config = yaml.safe_load(f)
                elif config_path.suffix.lower() == '.json':
                    file_config = json.load(f)
                else:
                    raise ValueError(f"Unsupported config file format: {config_path.suffix}")
            
            if file_config:
                # Merge with default configuration
                self._merge_config(file_config)
                logger.info(f"Configuration loaded from {config_path}")
            else:
                logger.warning(f"Empty configuration file: {config_path}")
                
        except Exception as e:
            logger.error(f"Failed to load configuration from {config_path}: {e}")
            raise
    
    def _merge_config(self, file_config: Dict[str, Any]) -> None:
        """Merge file configuration with defaults"""
        def merge_dict(default: dict, override: dict) -> dict:
            """Recursively merge dictionaries"""
            result = default.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = merge_dict(result[key], value)
                else:
                    result[key] = value
            return result
        
        self._config = merge_dict(self._config, file_config)
    
    def save_to_file(self, config_file: Union[str, Path], format: str = 'yaml') -> None:
        """
        Save current configuration to file.
        
        Args:
            config_file: Output file path
            format: File format ('yaml' or 'json')
        """
        config_path = Path(config_file)
        
        try:
            with open(config_path, 'w') as f:
                if format.lower() in ['yml', 'yaml']:
                    yaml.dump(self._config, f, default_flow_style=False, indent=2)
                elif format.lower() == 'json':
                    json.dump(self._config, f, indent=2)
                else:
                    raise ValueError(f"Unsupported format: {format}")
            
            logger.info(f"Configuration saved to {config_path}")
            
        except Exception as e:
            logger.error(f"Failed to save configuration: {e}")
            raise
    
    def _validate(self) -> None:
        """Validate configuration parameters"""
        errors = []
        
        # Validate serial settings
        serial_config = self._config.get('serial', {})
        if serial_config.get('baudrate', 0) <= 0:
            errors.append("Serial baudrate must be positive")
        if serial_config.get('timeout', 0) <= 0:
            errors.append("Serial timeout must be positive")
        
        # Validate ranges
        ranges = self._config.get('ranges', {})
        if ranges.get('ha_encoder_range', 0) <= 0:
            errors.append("HA encoder range must be positive")
        if ranges.get('dec_encoder_range', 0) <= 0:
            errors.append("Dec encoder range must be positive")
        
        # Validate coordinates
        coords = self._config.get('coordinates', {})
        lat = coords.get('observer_latitude', 0)
        if not (-90 <= lat <= 90):
            errors.append("Observer latitude must be between -90 and 90 degrees")
        if coords.get('dec_steps_per_degree', 0) <= 0:
            errors.append("Dec steps per degree must be positive")
        
        # Validate motion parameters
        motion = self._config.get('motion', {})
        for mode, params in motion.items():
            if isinstance(params, dict):
                for param, value in params.items():
                    if isinstance(value, (int, float)) and value <= 0:
                        errors.append(f"Motion parameter {mode}.{param} must be positive")
        
        # Validate safety parameters
        safety = self._config.get('safety', {})
        if safety.get('slew_timeout_seconds', 0) <= 0:
            errors.append("Slew timeout must be positive")
        
        if errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.debug("Configuration validation passed")
    
    # Configuration access methods
    def get_serial_config(self) -> Dict[str, Any]:
        """Get serial communication configuration"""
        return self._config['serial'].copy()
    
    def get_limits(self) -> Dict[str, Any]:
        """Get telescope encoder limits"""
        return self._config['limits'].copy()
    
    def get_ranges(self) -> Dict[str, Any]:
        """Get encoder ranges"""
        return self._config['ranges'].copy()
    
    def get_coordinates_config(self) -> Dict[str, Any]:
        """Get coordinate system configuration"""
        return self._config['coordinates'].copy()
    
    def get_motion_params(self, mode: str) -> Dict[str, Any]:
        """
        Get motion parameters for specified mode.
        
        Args:
            mode: Motion mode ('normal', 'fast', 'precise', 'initialization')
            
        Returns:
            Dictionary with motion parameters
        """
        if mode not in self._config['motion']:
            raise ValueError(f"Unknown motion mode: {mode}")
        return self._config['motion'][mode].copy()
    
    def get_tracking_config(self) -> Dict[str, Any]:
        """Get tracking configuration"""
        return self._config['tracking'].copy()
    
    def get_safety_config(self) -> Dict[str, Any]:
        """Get safety configuration"""
        return self._config['safety'].copy()
    
    def get_monitoring_config(self) -> Dict[str, Any]:
        """Get monitoring configuration"""
        return self._config['monitoring'].copy()
    
    def update_limits(self, ha_positive: int, dec_negative: int) -> None:
        """
        Update encoder limits (typically after initialization).
        
        Args:
            ha_positive: HA positive limit encoder value
            dec_negative: Dec negative limit encoder value
        """
        ranges = self.get_ranges()
        
        self._config['limits']['ha_positive'] = ha_positive
        self._config['limits']['dec_negative'] = dec_negative
        self._config['limits']['ha_negative'] = ha_positive - ranges['ha_encoder_range']
        self._config['limits']['dec_positive'] = dec_negative + ranges['dec_encoder_range']
        
        logger.info(f"Limits updated: HA+={ha_positive}, Dec-={dec_negative}")
        logger.info(f"Calculated: HA-={self._config['limits']['ha_negative']}, Dec+={self._config['limits']['dec_positive']}")
    
    def get_calibration_data(self) -> Dict[str, Any]:
        """
        Get calibration data in format expected by coordinates module.
        
        Returns:
            Dictionary with calibration data for Coordinates class
        """
        return {
            'observer_latitude': self._config['coordinates']['observer_latitude'],
            'limits': self._config['limits'],
            'ranges': self._config['ranges'],
            'dec_steps_per_degree': self._config['coordinates']['dec_steps_per_degree']
        }
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value by dot-notation key.
        
        Args:
            key: Configuration key (e.g., 'serial.port' or 'motion.normal.ha_velocity')
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        keys = key.split('.')
        value = self._config
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key: str, value: Any) -> None:
        """
        Set configuration value by dot-notation key.
        
        Args:
            key: Configuration key (e.g., 'serial.port')
            value: Value to set
        """
        keys = key.split('.')
        config = self._config
        
        # Navigate to parent dictionary
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        # Set the value
        config[keys[-1]] = value
        logger.debug(f"Configuration updated: {key} = {value}")
    
    def to_dict(self) -> Dict[str, Any]:
        """Get complete configuration as dictionary"""
        return self._config.copy()
    
    def __str__(self) -> str:
        """String representation"""
        return f"TelescopeConfig(port={self.get('serial.port')}, lat={self.get('coordinates.observer_latitude'):.3f}°)"