#!/usr/bin/env python3
"""
Interactive CLI for testing the ROTSE-III async telescope driver.
Provides comprehensive testing of all telescope functionality.
"""

import asyncio
import logging
import sys
import signal
from pathlib import Path
from typing import Optional, Dict, Any
import argparse
from datetime import datetime

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from async_telescope_driver import AsyncTelescopeDriver, SlewMode, InitializationResult
from state import MountState, TrackingMode, PierSide


class TelescopeCLI:
    """Interactive CLI for telescope control and testing"""
    
    def __init__(self, config_file: Optional[str] = None, port: Optional[str] = None, 
                 baudrate: Optional[int] = None, log_level: str = "INFO"):
        """Initialize CLI"""
        self.telescope: Optional[AsyncTelescopeDriver] = None
        self.config_file = config_file
        self.port = port
        self.baudrate = baudrate
        self.running = True
        
        # Setup logging
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler('telescope_cli.log')
            ]
        )
        self.logger = logging.getLogger(__name__)
        
        # Command mapping
        self.commands = {
            'help': self.cmd_help,
            'h': self.cmd_help,
            'connect': self.cmd_connect,
            'disconnect': self.cmd_disconnect,
            'status': self.cmd_status,
            'init': self.cmd_initialize,
            'initialize': self.cmd_initialize,
            'position': self.cmd_position,
            'pos': self.cmd_position,
            'slew': self.cmd_slew,
            'goto': self.cmd_slew,
            'stop': self.cmd_stop,
            'track': self.cmd_track,
            'untrack': self.cmd_untrack,
            'park': self.cmd_park,
            'unpark': self.cmd_unpark,
            'home': self.cmd_home,
            'limits': self.cmd_limits,
            'stats': self.cmd_communication_stats,
            'test': self.cmd_test_sequence,
            'parkinfo': self.cmd_park_info,
            'emergency': self.cmd_emergency_stop,
            'estop': self.cmd_emergency_stop,
            'config': self.cmd_show_config,
            'monitor': self.cmd_monitor,
            'raw': self.cmd_raw_command,
            'quit': self.cmd_quit,
            'exit': self.cmd_quit,
            'q': self.cmd_quit
        }
        
        print("🔭 ROTSE-III Async Telescope Driver CLI")
        print("=" * 50)
        print(f"Config: {config_file or 'default'}")
        print(f"Port: {port or 'from config'}")
        print(f"Baudrate: {baudrate or 'from config'}")
        print("Type 'help' for available commands")
        print("=" * 50)
    
    async def run(self):
        """Main CLI loop"""
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        try:
            while self.running:
                try:
                    # Get command input
                    command_line = await self._get_input("telescope> ")
                    
                    if not command_line.strip():
                        continue
                    
                    # Parse command and arguments
                    parts = command_line.strip().split()
                    cmd = parts[0].lower()
                    args = parts[1:] if len(parts) > 1 else []
                    
                    # Execute command
                    if cmd in self.commands:
                        try:
                            await self.commands[cmd](args)
                        except Exception as e:
                            print(f"❌ Command failed: {e}")
                            self.logger.error(f"Command '{cmd}' failed: {e}")
                    else:
                        print(f"❓ Unknown command: {cmd}. Type 'help' for available commands.")
                        
                except KeyboardInterrupt:
                    print("\n👋 Exiting...")
                    break
                except EOFError:
                    print("\n👋 Goodbye!")
                    break
                    
        finally:
            await self.cleanup()
    
    async def _get_input(self, prompt: str) -> str:
        """Get async input from user"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input, prompt)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        print(f"\n🛑 Received signal {signum}, shutting down gracefully...")
        self.running = False
    
    async def cleanup(self):
        """Cleanup resources"""
        if self.telescope and self.telescope.is_connected():
            print("🔌 Disconnecting telescope...")
            await self.telescope.disconnect()
            print("✅ Telescope disconnected")
    
    # Command implementations
    async def cmd_help(self, args):
        """Show help information"""
        print("\n📖 Available Commands:")
        print("=" * 50)
        
        commands_help = {
            "Connection": {
                "connect": "Connect to telescope",
                "disconnect": "Disconnect from telescope",
                "status": "Show telescope status"
            },
            "Setup": {
                "init": "Initialize telescope (home and calibrate)",
                "config": "Show current configuration",
                "limits": "Show encoder limits"
            },
            "Positioning": {
                "position, pos": "Show current position",
                "slew <ha> <dec> [mode]": "Slew to HA/Dec (hours, degrees)",
                "goto <ha> <dec>": "Alias for slew",
                "stop": "Stop all motion",
                "home": "Go to home position"
            },
            "Tracking": {
                "track": "Start sidereal tracking",
                "untrack": "Stop tracking",
                "park [ha] [dec]": "Park telescope at position (default: 0h -20°)",
                "unpark": "Unpark telescope",
                "parkinfo": "Show park position information"
            },
            "Testing": {
                "test": "Run comprehensive test sequence",
                "monitor [duration]": "Monitor position for duration (seconds)",
                "stats": "Show communication statistics",
                "raw <command>": "Send raw command to telescope"
            },
            "Safety": {
                "emergency, estop": "Emergency stop all motion"
            },
            "System": {
                "help, h": "Show this help",
                "quit, exit, q": "Exit CLI"
            }
        }
        
        for category, cmds in commands_help.items():
            print(f"\n{category}:")
            for cmd, desc in cmds.items():
                print(f"  {cmd:<20} - {desc}")
        
        print(f"\n💡 Examples:")
        print(f"  telescope> connect")
        print(f"  telescope> init")
        print(f"  telescope> slew 2.5 -30.0 fast")
        print(f"  telescope> track")
        print(f"  telescope> park 1.0 -15.0")
        print(f"  telescope> parkinfo")
        print(f"  telescope> unpark")
        print(f"  telescope> monitor 30")
        print()
    
    async def cmd_connect(self, args):
        """Connect to telescope"""
        if self.telescope and self.telescope.is_connected():
            print("⚠️  Already connected to telescope")
            return
        
        print("🔌 Connecting to telescope...")
        
        try:
            self.telescope = AsyncTelescopeDriver(
                config_file=self.config_file,
                port=self.port,
                baudrate=self.baudrate
            )
            
            success = await self.telescope.connect()
            if success:
                print("✅ Connected to telescope successfully!")
                await self._show_connection_info()
            else:
                print("❌ Failed to connect to telescope")
                self.telescope = None
                
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            self.telescope = None
    
    async def cmd_disconnect(self, args):
        """Disconnect from telescope"""
        if not self.telescope:
            print("⚠️  No telescope connection")
            return
        
        print("🔌 Disconnecting from telescope...")
        await self.telescope.disconnect()
        self.telescope = None
        print("✅ Disconnected successfully")
    
    async def cmd_status(self, args):
        """Show comprehensive telescope status"""
        if not await self._check_connection():
            return
        
        status = self.telescope.get_status()
        
        print("\n📊 Telescope Status:")
        print("=" * 40)
        print(f"State:           {status['state'].upper()}")
        print(f"Tracking:        {status['tracking_mode'].upper()}")
        print(f"Initialized:     {'✅' if status['initialized'] else '❌'}")
        print(f"Connected:       {'✅' if status['connected'] else '❌'}")
        
        print(f"\n📍 Position:")
        pos = status['position']
        if pos['ha'] is not None and pos['dec'] is not None:
            print(f"  Hour Angle:    {pos['ha']:8.4f} hours")
            print(f"  Declination:   {pos['dec']:8.3f} degrees")
            print(f"  Pier Side:     {pos['pier_side'].upper()}")
        else:
            print("  Position:      Unknown")
        
        print(f"\n🔢 Encoders:")
        enc = status['encoders']
        if enc['ha'] is not None and enc['dec'] is not None:
            print(f"  HA Encoder:    {enc['ha']:10d} steps")
            print(f"  Dec Encoder:   {enc['dec']:10d} steps")
        else:
            print("  Encoders:      Unknown")
        
        comm = status['communication']
        print(f"\n📡 Communication:")
        print(f"  Port:          {comm['port']}")
        print(f"  Success Rate:  {comm['success_rate']}")
        print(f"  Commands Sent: {comm['commands_sent']}")
        print(f"  Timeouts:      {comm['timeouts']}")
        print()
    
    async def cmd_initialize(self, args):
        """Initialize telescope"""
        if not await self._check_connection():
            return
        
        move_to_safe = True
        if args and args[0].lower() in ['no-move', 'nomove', 'stay']:
            move_to_safe = False
        
        print("🏠 Initializing telescope...")
        print("⚠️  This will move telescope to encoder limits!")
        
        confirm = await self._get_input("Continue? [y/N]: ")
        if confirm.lower() not in ['y', 'yes']:
            print("❌ Initialization cancelled")
            return
        
        try:
            result = await self.telescope.initialize(move_to_safe_position=move_to_safe)
            
            if result.success:
                print("✅ Initialization completed successfully!")
                print(f"⏱️  Duration: {result.duration_seconds:.1f} seconds")
                if result.home_ha_encoder is not None:
                    print(f"🏠 Home positions: HA={result.home_ha_encoder}, Dec={result.home_dec_encoder}")
            else:
                print(f"❌ Initialization failed: {result.message}")
                
        except Exception as e:
            print(f"❌ Initialization error: {e}")
    
    async def cmd_position(self, args):
        """Show current position"""
        if not await self._check_connection():
            return
        
        ha, dec = self.telescope.get_position()
        
        if ha is not None and dec is not None:
            print(f"📍 Current Position:")
            print(f"  Hour Angle:  {ha:8.4f} hours  ({ha*15:7.3f}°)")
            print(f"  Declination: {dec:8.3f} degrees")
            
            # Show pier side
            pier_side = self.telescope.status.pier_side
            print(f"  Pier Side:   {pier_side.value.upper()}")
            
            # Show time to meridian
            time_to_meridian = abs(ha)
            print(f"  Time to Meridian: {time_to_meridian:.3f} hours")
            
        else:
            print("❓ Position unknown")
    
    async def cmd_slew(self, args):
        """Slew to specified coordinates"""
        if not await self._check_connection():
            return
        
        if not self.telescope.is_initialized():
            print("❌ Telescope not initialized. Run 'init' first.")
            return
        
        if len(args) < 2:
            print("❌ Usage: slew <hour_angle> <declination> [mode]")
            print("   Modes: fast, normal, precise (default: normal)")
            return
        
        try:
            ha = float(args[0])
            dec = float(args[1])
            mode_str = args[2].lower() if len(args) > 2 else 'normal'
            
            # Validate mode
            try:
                mode = SlewMode(mode_str)
            except ValueError:
                print(f"❌ Invalid mode '{mode_str}'. Use: fast, normal, precise")
                return
            
            print(f"🎯 Slewing to HA={ha:.4f}h, Dec={dec:.3f}° in {mode.value} mode...")
            
            success = await self.telescope.slew_to_coordinates(ha, dec, mode)
            
            if success:
                print("✅ Slew started successfully!")
                print("💡 Use 'monitor' to watch progress or 'stop' to halt")
            else:
                print("❌ Failed to start slew")
                
        except ValueError:
            print("❌ Invalid coordinates. Use numeric values.")
        except Exception as e:
            print(f"❌ Slew failed: {e}")
    
    async def cmd_stop(self, args):
        """Stop all telescope motion"""
        if not await self._check_connection():
            return
        
        print("🛑 Stopping telescope...")
        success = await self.telescope.stop()
        
        if success:
            print("✅ Telescope stopped successfully")
        else:
            print("❌ Failed to stop telescope")
    
    async def cmd_track(self, args):
        """Start sidereal tracking"""
        if not await self._check_connection():
            return
        
        if not self.telescope.is_initialized():
            print("❌ Telescope not initialized. Run 'init' first.")
            return
        
        print("🌟 Starting sidereal tracking...")
        success = await self.telescope.start_tracking(TrackingMode.SIDEREAL)
        
        if success:
            print("✅ Sidereal tracking started!")
            print("💡 Use 'untrack' to stop tracking")
        else:
            print("❌ Failed to start tracking")
    
    async def cmd_untrack(self, args):
        """Stop tracking"""
        if not await self._check_connection():
            return
        
        print("🛑 Stopping tracking...")
        success = await self.telescope.stop()
        
        if success:
            print("✅ Tracking stopped")
        else:
            print("❌ Failed to stop tracking")
    
    async def cmd_park(self, args):
        """Park telescope at specified coordinates"""
        if not await self._check_connection():
            return
        
        if not self.telescope.is_initialized():
            print("❌ Telescope not initialized. Run 'init' first.")
            return
        
        # Parse optional coordinates
        ha = 0.0  # Default: on meridian
        dec = -20.0  # Default: pointing south
        
        if len(args) >= 1:
            try:
                ha = float(args[0])
            except ValueError:
                print("❌ Invalid hour angle. Using default 0.0h")
        
        if len(args) >= 2:
            try:
                dec = float(args[1])
            except ValueError:
                print("❌ Invalid declination. Using default -20.0°")
        
        print(f"🅿️  Parking telescope at HA={ha:.3f}h, Dec={dec:.1f}°...")
        success = await self.telescope.park(ha, dec)
        
        if success:
            print("✅ Parking sequence started!")
            print("💡 Use 'monitor' to watch progress or 'status' to check state")
            print("🔍 Telescope will be in PARKING state until movement completes")
        else:
            print("❌ Failed to start parking")
    
    async def cmd_unpark(self, args):
        """Unpark telescope"""
        if not await self._check_connection():
            return
        
        if not self.telescope.is_parked():
            current_state = self.telescope.status.state.value
            print(f"❌ Cannot unpark: telescope is in '{current_state}' state, not 'parked'")
            return
        
        print("🅿️  Unparking telescope...")
        success = await self.telescope.unpark()
        
        if success:
            print("✅ Telescope unparked - ready for operations!")
        else:
            print("❌ Failed to unpark telescope")
    
    async def cmd_park_info(self, args):
        """Show park position information"""
        if not await self._check_connection():
            return
        
        print("\n🅿️  Park Information:")
        print("=" * 30)
        
        # Current state
        current_state = self.telescope.status.state
        is_parked = self.telescope.is_parked()
        
        print(f"Current State:    {current_state.value.upper()}")
        print(f"Is Parked:        {'✅ Yes' if is_parked else '❌ No'}")
        
        # Park position
        park_pos = self.telescope.get_park_position()
        if park_pos:
            ha, dec = park_pos
            print(f"Park Position:    HA={ha:.3f}h, Dec={dec:.1f}°")
            print(f"Park Position:    RA={ha*15:.3f}°, Dec={dec:.1f}°")
        else:
            print("Park Position:    Not set")
        
        # Current position for comparison
        current_ha, current_dec = self.telescope.get_position()
        if current_ha is not None and current_dec is not None:
            print(f"Current Position: HA={current_ha:.3f}h, Dec={current_dec:.1f}°")
            
            # Distance to park position
            if park_pos:
                park_ha, park_dec = park_pos
                ha_diff = abs(current_ha - park_ha)
                dec_diff = abs(current_dec - park_dec)
                print(f"Distance to Park: ΔHA={ha_diff:.3f}h, ΔDec={dec_diff:.1f}°")
        else:
            print("Current Position: Unknown")
        
        # Show default park position
        print(f"\nDefault Park:     HA=0.000h, Dec=-20.0° (meridian, south)")
        
        # Usage instructions
        print(f"\n💡 Usage:")
        print(f"  park              - Park at default position (0h, -20°)")
        print(f"  park 1.5 -30      - Park at HA=1.5h, Dec=-30°")
        print(f"  unpark            - Unpark telescope (PARKED → IDLE)")
        print(f"  monitor           - Watch parking progress")
    
    async def cmd_home(self, args):
        """Go to home position"""
        if not await self._check_connection():
            return
        
        if not self.telescope.is_initialized():
            print("❌ Telescope not initialized. Run 'init' first.")
            return
        
        print("🏠 Moving to home position (HA=6h, Dec at negative limit)...")
        success = await self.telescope.slew_to_coordinates(6.0, -45.0, SlewMode.NORMAL)
        
        if success:
            print("✅ Slewing to home position!")
        else:
            print("❌ Failed to start slew to home")
    
    async def cmd_limits(self, args):
        """Show encoder limits"""
        if not await self._check_connection():
            return
        
        if not self.telescope.coords:
            print("❌ Coordinate system not available")
            return
        
        limits = self.telescope.config.get_limits()
        ranges = self.telescope.config.get_ranges()
        
        print(f"\n📏 Encoder Limits:")
        print(f"  HA Positive:   {limits.get('ha_positive', 'Unknown'):10}")
        print(f"  HA Negative:   {limits.get('ha_negative', 'Unknown'):10}")
        print(f"  Dec Positive:  {limits.get('dec_positive', 'Unknown'):10}")
        print(f"  Dec Negative:  {limits.get('dec_negative', 'Unknown'):10}")
        
        print(f"\n📐 Encoder Ranges:")
        print(f"  HA Range:      {ranges['ha_encoder_range']:10} steps")
        print(f"  Dec Range:     {ranges['dec_encoder_range']:10} steps")
        
        # Show safety margins
        safety_config = self.telescope.config.get_safety_config()
        safety_margin = safety_config.get('safety_margin_steps', 20000)
        print(f"\n⚠️  Safety Margin: {safety_margin} steps")
    
    async def cmd_communication_stats(self, args):
        """Show communication statistics"""
        if not await self._check_connection():
            return
        
        stats = self.telescope.comm.get_statistics()
        
        print(f"\n📡 Communication Statistics:")
        print(f"  Port:              {stats['port']}")
        print(f"  Connected:         {'✅' if stats['connected'] else '❌'}")
        print(f"  Commands Sent:     {stats['commands_sent']}")
        print(f"  Responses:         {stats['responses_received']}")
        print(f"  Success Rate:      {stats['success_rate']}")
        print(f"  Timeouts:          {stats['timeouts']}")
        print(f"  CRC Errors:        {stats['crc_errors']}")
        print(f"  Buffer Flushes:    {stats['buffer_flushes']}")
    
    async def cmd_test_sequence(self, args):
        """Run comprehensive test sequence"""
        if not await self._check_connection():
            return
        
        print("🧪 Running comprehensive test sequence...")
        print("=" * 50)
        
        try:
            # Test 1: Communication
            print("\n1️⃣  Testing communication...")
            comm_test = await self.telescope.comm.test_communication()
            print(f"   Result: {'✅ PASS' if comm_test else '❌ FAIL'}")
            
            # Test 2: Status update
            print("\n2️⃣  Testing status updates...")
            await self.telescope._update_telescope_status()
            ha_pos = self.telescope.status.ra_axis.encoder_position
            dec_pos = self.telescope.status.dec_axis.encoder_position
            status_test = ha_pos is not None and dec_pos is not None
            print(f"   Result: {'✅ PASS' if status_test else '❌ FAIL'}")
            if status_test:
                print(f"   HA Encoder: {ha_pos}, Dec Encoder: {dec_pos}")
            
            # Test 3: Coordinate conversion (if initialized)
            if self.telescope.is_initialized():
                print("\n3️⃣  Testing coordinate conversion...")
                ha, dec = self.telescope.get_position()
                coord_test = ha is not None and dec is not None
                print(f"   Result: {'✅ PASS' if coord_test else '❌ FAIL'}")
                if coord_test:
                    print(f"   Position: HA={ha:.4f}h, Dec={dec:.3f}°")
            else:
                print("\n3️⃣  Skipping coordinate test (not initialized)")
            
            # Test 4: Motion commands (if initialized)
            if self.telescope.is_initialized():
                print("\n4️⃣  Testing motion commands...")
                original_ha, original_dec = self.telescope.get_position()
                
                if original_ha is not None and original_dec is not None:
                    # Small slew test
                    test_ha = original_ha + 0.1  # Move 0.1 hours east
                    test_dec = original_dec + 1.0  # Move 1 degree north
                    
                    print(f"   Moving to test position: HA={test_ha:.4f}h, Dec={test_dec:.3f}°")
                    slew_test = await self.telescope.slew_to_coordinates(test_ha, test_dec, SlewMode.PRECISE)
                    
                    if slew_test:
                        # Wait a bit for motion to start
                        await asyncio.sleep(2.0)
                        await self.telescope.stop()
                        print(f"   Result: ✅ PASS (motion commands accepted)")
                        
                        # Return to original position
                        print(f"   Returning to original position...")
                        await self.telescope.slew_to_coordinates(original_ha, original_dec, SlewMode.PRECISE)
                        await asyncio.sleep(2.0)
                        await self.telescope.stop()
                    else:
                        print(f"   Result: ❌ FAIL (motion command rejected)")
                else:
                    print(f"   Result: ❌ SKIP (position unknown)")
            else:
                print("\n4️⃣  Skipping motion test (not initialized)")
            
            print("\n✅ Test sequence completed!")
            
        except Exception as e:
            print(f"\n❌ Test sequence failed: {e}")
    
    async def cmd_monitor(self, args):
        """Monitor telescope position"""
        if not await self._check_connection():
            return
        
        duration = 30.0  # Default 30 seconds
        if args:
            try:
                duration = float(args[0])
            except ValueError:
                print("❌ Invalid duration. Using 30 seconds.")
        
        print(f"👁️  Monitoring telescope for {duration} seconds...")
        print("Press Ctrl+C to stop early")
        print("=" * 60)
        
        start_time = asyncio.get_event_loop().time()
        
        try:
            while (asyncio.get_event_loop().time() - start_time) < duration:
                # Update status
                await self.telescope._update_telescope_status()
                
                # Get current info
                ha, dec = self.telescope.get_position()
                state = self.telescope.status.state
                
                # Get encoder positions
                ha_enc = self.telescope.status.ra_axis.encoder_position
                dec_enc = self.telescope.status.dec_axis.encoder_position
                
                # Format display
                timestamp = datetime.now().strftime("%H:%M:%S")
                
                if ha is not None and dec is not None:
                    print(f"{timestamp} | {state.value:8s} | HA: {ha:7.4f}h | Dec: {dec:7.3f}° | RA_enc: {ha_enc or 0:8d} | Dec_enc: {dec_enc or 0:8d}")
                else:
                    print(f"{timestamp} | {state.value:8s} | Position: Unknown")
                
                await asyncio.sleep(1.0)
                
        except KeyboardInterrupt:
            print("\n⏹️  Monitoring stopped by user")
        
        print("✅ Monitoring completed")
    
    async def cmd_raw_command(self, args):
        """Send raw command to telescope"""
        if not await self._check_connection():
            return
        
        if not args:
            print("❌ Usage: raw <command>")
            print("   Example: raw $Status1RA")
            return
        
        command = ' '.join(args)
        print(f"📤 Sending raw command: {command}")
        
        try:
            response = await self.telescope.comm.send_command(command)
            if response is not None:
                print(f"📥 Response: {response}")
            else:
                print("❌ No response received")
                
        except Exception as e:
            print(f"❌ Command failed: {e}")
    
    async def cmd_emergency_stop(self, args):
        """Emergency stop all motion"""
        if not await self._check_connection():
            return
        
        print("🚨 EMERGENCY STOP!")
        success = await self.telescope.comm.emergency_stop()
        
        if success:
            print("✅ Emergency stop commands sent")
        else:
            print("❌ Emergency stop failed")
    
    async def cmd_show_config(self, args):
        """Show current configuration"""
        if not self.telescope:
            print("❌ No telescope connection")
            return
        
        config = self.telescope.config.to_dict()
        
        print(f"\n⚙️  Telescope Configuration:")
        print("=" * 40)
        
        # Serial settings
        serial = config['serial']
        print(f"Serial:")
        print(f"  Port:        {serial['port']}")
        print(f"  Baudrate:    {serial['baudrate']}")
        print(f"  Timeout:     {serial['timeout']}s")
        
        # Coordinates
        coords = config['coordinates']
        print(f"\nCoordinates:")
        print(f"  Latitude:    {coords['observer_latitude']:.4f}°")
        print(f"  Steps/Deg:   {coords['dec_steps_per_degree']:.1f}")
        
        # Motion parameters
        motion = config['motion']['normal']
        print(f"\nMotion (Normal):")
        print(f"  HA Velocity: {motion['ha_velocity']:5d} steps/sec")
        print(f"  Dec Velocity:{motion['dec_velocity']:5d} steps/sec")
        
        # Safety
        safety = config['safety']
        print(f"\nSafety:")
        print(f"  Slew Timeout:     {safety['slew_timeout_seconds']:.1f}s")
        print(f"  Position Tolerance: {safety['position_tolerance_steps']:d} steps")
        print(f"  Safety Margin:    {safety['safety_margin_steps']:d} steps")
    
    async def cmd_quit(self, args):
        """Quit the CLI"""
        print("👋 Goodbye!")
        self.running = False
    
    # Helper methods
    async def _check_connection(self) -> bool:
        """Check if telescope is connected"""
        if not self.telescope or not self.telescope.is_connected():
            print("❌ Not connected to telescope. Use 'connect' first.")
            return False
        return True
    
    async def _show_connection_info(self):
        """Show connection information"""
        if not self.telescope:
            return
        
        config = self.telescope.config
        print(f"📡 Connection Details:")
        print(f"  Port:      {config.get('serial.port')}")
        print(f"  Baudrate:  {config.get('serial.baudrate')}")
        print(f"  Latitude:  {config.get('coordinates.observer_latitude'):.4f}°")


async def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(description='ROTSE-III Telescope CLI')
    parser.add_argument('--config', '-c', help='Configuration file path')
    parser.add_argument('--port', '-p', help='Serial port override')
    parser.add_argument('--baudrate', '-b', type=int, help='Baudrate override')
    parser.add_argument('--log-level', '-l', default='INFO', 
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Log level')
    
    args = parser.parse_args()
    
    # Create and run CLI
    cli = TelescopeCLI(
        config_file=args.config,
        port=args.port,
        baudrate=args.baudrate,
        log_level=args.log_level
    )
    
    try:
        await cli.run()
    except Exception as e:
        print(f"❌ CLI error: {e}")
        logging.error(f"CLI error: {e}")
    finally:
        await cli.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)