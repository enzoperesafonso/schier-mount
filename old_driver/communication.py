"""
Robust, thread-safe serial communication module for ROTSE-III telescope mount.
Handles desync issues with comprehensive error recovery and command queuing.
"""

import asyncio
import logging
import time
import threading
from typing import Tuple, Optional, Dict, Any, List
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import serial
import json
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class CommandRequest:
    """Represents a single command request with its expected response"""
    command: str
    expected_response_prefix: str
    timeout: float = 5.0
    retries: int = 2
    priority: int = 0  # Higher number = higher priority

class CRC16:
    """CRC16 calculation for ROTSE-III protocol"""
    
    # CRC lookup table for ROTSE-III protocol
    TABLE = [
        0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50a5, 0x60c6, 0x70e7,
        0x8108, 0x9129, 0xa14a, 0xb16b, 0xc18c, 0xd1ad, 0xe1ce, 0xf1ef,
        0x1231, 0x0210, 0x3273, 0x2252, 0x52b5, 0x4294, 0x72f7, 0x62d6,
        0x9339, 0x8318, 0xb37b, 0xa35a, 0xd3bd, 0xc39c, 0xf3ff, 0xe3de,
        0x2462, 0x3443, 0x0420, 0x1401, 0x64e6, 0x74c7, 0x44a4, 0x5485,
        0xa56a, 0xb54b, 0x8528, 0x9509, 0xe5ee, 0xf5cf, 0xc5ac, 0xd58d,
        0x3653, 0x2672, 0x1611, 0x0630, 0x76d7, 0x66f6, 0x5695, 0x46b4,
        0xb75b, 0xa77a, 0x9719, 0x8738, 0xf7df, 0xe7fe, 0xd79d, 0xc7bc,
        0x48c4, 0x58e5, 0x6886, 0x78a7, 0x0840, 0x1861, 0x2802, 0x3823,
        0xc9cc, 0xd9ed, 0xe98e, 0xf9af, 0x8948, 0x9969, 0xa90a, 0xb92b,
        0x5af5, 0x4ad4, 0x7ab7, 0x6a96, 0x1a71, 0x0a50, 0x3a33, 0x2a12,
        0xdbfd, 0xcbdc, 0xfbbf, 0xeb9e, 0x9b79, 0x8b58, 0xbb3b, 0xab1a,
        0x6ca6, 0x7c87, 0x4ce4, 0x5cc5, 0x2c22, 0x3c03, 0x0c60, 0x1c41,
        0xedae, 0xfd8f, 0xcdec, 0xddcd, 0xad2a, 0xbd0b, 0x8d68, 0x9d49,
        0x7e97, 0x6eb6, 0x5ed5, 0x4ef4, 0x3e13, 0x2e32, 0x1e51, 0x0e70,
        0xff9f, 0xefbe, 0xdfdd, 0xcffc, 0xbf1b, 0xaf3a, 0x9f59, 0x8f78,
        0x9188, 0x81a9, 0xb1ca, 0xa1eb, 0xd10c, 0xc12d, 0xf14e, 0xe16f,
        0x1080, 0x00a1, 0x30c2, 0x20e3, 0x5004, 0x4025, 0x7046, 0x6067,
        0x83b9, 0x9398, 0xa3fb, 0xb3da, 0xc33d, 0xd31c, 0xe37f, 0xf35e,
        0x02b1, 0x1290, 0x22f3, 0x32d2, 0x4235, 0x5214, 0x6277, 0x7256,
        0xb5ea, 0xa5cb, 0x95a8, 0x8589, 0xf56e, 0xe54f, 0xd52c, 0xc50d,
        0x34e2, 0x24c3, 0x14a0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
        0xa7db, 0xb7fa, 0x8799, 0x97b8, 0xe75f, 0xf77e, 0xc71d, 0xd73c,
        0x26d3, 0x36f2, 0x0691, 0x16b0, 0x6657, 0x7676, 0x4615, 0x5634,
        0xd94c, 0xc96d, 0xf90e, 0xe92f, 0x99c8, 0x89e9, 0xb98a, 0xa9ab,
        0x5844, 0x4865, 0x7806, 0x6827, 0x18c0, 0x08e1, 0x3882, 0x28a3,
        0xcb7d, 0xdb5c, 0xeb3f, 0xfb1e, 0x8bf9, 0x9bd8, 0xabbb, 0xbb9a,
        0x4a75, 0x5a54, 0x6a37, 0x7a16, 0x0af1, 0x1ad0, 0x2ab3, 0x3a92,
        0xfd2e, 0xed0f, 0xdd6c, 0xcd4d, 0xbdaa, 0xad8b, 0x9de8, 0x8dc9,
        0x7c26, 0x6c07, 0x5c64, 0x4c45, 0x3ca2, 0x2c83, 0x1ce0, 0x0cc1,
        0xef1f, 0xff3e, 0xcf5d, 0xdf7c, 0xaf9b, 0xbfba, 0x8fd9, 0x9ff8,
        0x6e17, 0x7e36, 0x4e55, 0x5e74, 0x2e93, 0x3eb2, 0x0ed1, 0x1ef0
    ]
    
    @classmethod
    def calculate(cls, data: str) -> int:
        """Calculate CRC16 for given string"""
        crc = 0
        for char in data:
            crc = cls.TABLE[((crc >> 8) & 255)] ^ (crc << 8) ^ ord(char)
        return crc & 0xFFFF
    
    @classmethod
    def append_to_command(cls, command: str) -> str:
        """Append CRC and carriage return to command"""
        crc_value = cls.calculate(command)
        return f"{command}{crc_value:04X}\r"
    
    @classmethod
    def validate_response(cls, response: str) -> bool:
        """Validate CRC of received response"""
        if len(response) < 6:  # Minimum: @cmd + 4 char CRC
            return False
        
        # Remove \r if present
        response = response.rstrip('\r\n')
        if len(response) < 4:
            return False
            
        data = response[:-4]
        received_crc = response[-4:]
        calculated_crc = cls.calculate(data)
        
        return f"{calculated_crc:04X}".upper() == received_crc.upper()

class SerialBufferManager:
    """Manages serial buffer with robust desync recovery"""
    
    def __init__(self, serial_port: serial.Serial):
        self._serial = serial_port
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._last_flush = time.time()
        
    def flush_buffer(self) -> None:
        """Clear buffer and flush serial port"""
        with self._lock:
            logger.info("Flushing serial buffer for desync recovery")
            try:
                # Clear our buffer
                self._buffer.clear()
                
                # Flush both input and output buffers
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
                
                # Read any remaining data with short timeout
                old_timeout = self._serial.timeout
                self._serial.timeout = 0.1
                
                bytes_flushed = 0
                while True:
                    data = self._serial.read(1024)
                    if not data:
                        break
                    bytes_flushed += len(data)
                    
                self._serial.timeout = old_timeout
                self._last_flush = time.time()
                
                if bytes_flushed > 0:
                    logger.warning(f"Flushed {bytes_flushed} bytes from serial buffer")
                else:
                    logger.info("Serial buffer was already empty")
                    
            except Exception as e:
                logger.error(f"Error flushing serial buffer: {e}")
                raise

    def read_until_terminator(self, terminator: bytes = b'\r', timeout: float = 5.0) -> bytes:
        """Read until terminator with proper buffer management"""
        start_time = time.time()

        with self._lock:
            while time.time() - start_time < timeout:
                # Check if terminator is already in buffer
                term_idx = self._buffer.find(terminator)
                if term_idx >= 0:
                    # Extract complete message
                    message = bytes(self._buffer[:term_idx + len(terminator)])
                    del self._buffer[:term_idx + len(terminator)]
                    return message

                # Read more data from serial port
                try:
                    remaining_time = timeout - (time.time() - start_time)
                    if remaining_time <= 0:
                        break

                    old_timeout = self._serial.timeout
                    self._serial.timeout = min(0.5, remaining_time)

                    new_data = self._serial.read(1024)
                    self._serial.timeout = old_timeout

                    if new_data:
                        self._buffer.extend(new_data)
                    else:
                        time.sleep(0.01)  # Small delay to prevent busy loop

                except Exception as e:
                    logger.error(f"Error reading from serial port: {e}")
                    break

            # Timeout reached
            buffer_content = bytes(self._buffer).decode('ascii', errors='ignore')
            logger.warning(f"Read timeout after {timeout}s. Buffer: '{buffer_content[:100]}...'")
            raise TimeoutError("Read operation timed out")

    def should_auto_flush(self, max_age: float = 30.0) -> bool:
        """Check if buffer should be auto-flushed"""
        with self._lock:
            return (time.time() - self._last_flush > max_age and len(self._buffer) > 0)

class TelescopeSerial:
    """Robust serial communication with the ROTSE-III telescope mount"""

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600):
        self.device = device
        self.baudrate = baudrate
        self._serial: Optional[serial.Serial] = None
        self._buffer_manager: Optional[SerialBufferManager] = None

        # Thread safety
        self._command_lock = threading.RLock()
        self._connected = False
        self._shutdown_event = threading.Event()

        # Command queuing for priority handling
        self._command_queue = []
        self._queue_lock = threading.RLock()
        self._queue_condition = threading.Condition(self._queue_lock)

        # Background worker thread
        self._worker_thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telescope_serial")

        # Statistics and monitoring
        self._stats = {
            'commands_sent': 0,
            'responses_received': 0,
            'crc_errors': 0,
            'timeouts': 0,
            'buffer_flushes': 0
        }

        logger.info(f"TelescopeSerial initialized for {device} at {baudrate} baud")

    def connect(self) -> bool:
        """Establish serial connection"""
        try:
            logger.info(f"Connecting to {self.device} at {self.baudrate} baud")

            self._serial = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=2.0,
                write_timeout=2.0,
                rtscts=False,  # No hardware flow control
                dsrdtr=False,  # No DTR/DSR control
                xonxoff=False  # No software flow control
            )

            if not self._serial.is_open:
                self._serial.open()

            self._buffer_manager = SerialBufferManager(self._serial)
            self._connected = True

            # Start background worker thread
            self._start_worker_thread()

            logger.info("Serial connection established successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Close serial connection and cleanup"""
        logger.info("Disconnecting serial communication")

        self._connected = False
        self._shutdown_event.set()

        # Stop worker thread
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
            if self._worker_thread.is_alive():
                logger.warning("Worker thread did not shut down gracefully")

        # Close serial port
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                logger.error(f"Error closing serial port: {e}")

        # Shutdown executor
        self._executor.shutdown(wait=True)

        logger.info("Serial communication disconnected")

    def _start_worker_thread(self) -> None:
        """Start background worker thread for command processing"""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(
                target=self._command_worker,
                name="telescope_command_worker",
                daemon=True
            )
            self._worker_thread.start()
            logger.info("Command worker thread started")

    def _command_worker(self) -> None:
        """Background worker that processes commands sequentially"""
        logger.info("Command worker starting")

        while not self._shutdown_event.is_set() and self._connected:
            try:
                # Wait for commands or timeout
                with self._queue_condition:
                    if not self._command_queue:
                        self._queue_condition.wait(timeout=1.0)
                        continue

                    # Get highest priority command
                    self._command_queue.sort(key=lambda x: (-x[2].priority, x[0]))  # Sort by priority, then by time
                    timestamp, future, request = self._command_queue.pop(0)

                # Execute command if not cancelled
                if not future.cancelled():
                    try:
                        result = self._execute_command(request)
                        future.set_result(result)
                    except Exception as e:
                        future.set_exception(e)

                # Auto-flush buffer if needed
                if self._buffer_manager and self._buffer_manager.should_auto_flush():
                    self._buffer_manager.flush_buffer()
                    self._stats['buffer_flushes'] += 1

            except Exception as e:
                logger.error(f"Error in command worker: {e}")
                time.sleep(0.1)

        logger.info("Command worker thread exiting")

    def _execute_command(self, request: CommandRequest) -> str:
        """Execute a single command with retries and proper error handling"""
        if not self._connected or not self._serial or not self._buffer_manager:
            raise RuntimeError("Serial port not connected")

        with self._command_lock:
            command_with_crc = CRC16.append_to_command(request.command)
            logger.debug(f"Executing command: {request.command}")

            for attempt in range(request.retries + 1):
                try:
                    # Flush buffer on retry
                    if attempt > 0:
                        logger.warning(f"Retrying command {request.command} (attempt {attempt + 1})")
                        self._buffer_manager.flush_buffer()
                        self._stats['buffer_flushes'] += 1
                        time.sleep(0.1)

                    # Send command
                    self._serial.write(command_with_crc.encode())
                    self._serial.flush()
                    self._stats['commands_sent'] += 1

                    # Small delay to ensure command is processed
                    time.sleep(0.05)

                    # Read response
                    response_bytes = self._buffer_manager.read_until_terminator(
                        timeout=request.timeout
                    )
                    response_str = response_bytes.decode('ascii', errors='ignore').strip()

                    logger.debug(f"Received response: '{response_str}'")
                    self._stats['responses_received'] += 1

                    # Validate response
                    if self._validate_response(request, response_str):
                        return self._extract_response_data(request, response_str)
                    else:
                        self._stats['crc_errors'] += 1
                        if attempt == request.retries:
                            logger.error(f"All retries failed for command: {request.command}")
                            return ""
                        continue

                except TimeoutError:
                    self._stats['timeouts'] += 1
                    logger.error(f"Timeout on command {request.command} (attempt {attempt + 1})")
                    if attempt == request.retries:
                        raise

                except Exception as e:
                    logger.error(f"Error executing command {request.command} (attempt {attempt + 1}): {e}")
                    if attempt == request.retries:
                        raise

            return ""  # All attempts failed

    def _validate_response(self, request: CommandRequest, response: str) -> bool:
        """Validate response format and CRC"""
        # Validate CRC
        if not CRC16.validate_response(response):
            logger.warning(f"CRC validation failed: {response}")
            return False

        # Check expected response prefix
        if not response.startswith(request.expected_response_prefix):
            logger.warning(f"Response prefix mismatch. Expected: '{request.expected_response_prefix}', got: '{response[:20]}...'")
            return False

        # Check for error responses (contain '?' after command)
        if '?' in response and request.expected_response_prefix in response:
            logger.error(f"Mount returned error: {response}")
            return False

        return True

    def _extract_response_data(self, request: CommandRequest, response: str) -> str:
        """Extract data portion from validated response"""
        # Remove CRC (last 4 characters) and expected prefix
        response = response.rstrip('\r\n')
        data = response[len(request.expected_response_prefix):-4].strip()
        return data

    def send_command(self, command: str, expected_response_prefix: str,
                    timeout: float = 5.0, retries: int = 2, priority: int = 0) -> str:
        """Send command and return response (thread-safe)"""
        if not self._connected:
            raise RuntimeError("Serial port not connected")

        request = CommandRequest(
            command=command,
            expected_response_prefix=expected_response_prefix,
            timeout=timeout,
            retries=retries,
            priority=priority
        )

        # Execute immediately in current thread (simplified approach)
        try:
            result = self._execute_command(request)
            return result
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            raise

    def emergency_flush(self) -> None:
        """Emergency buffer flush for desync recovery"""
        if self._buffer_manager:
            logger.warning("Emergency flush requested")
            self._buffer_manager.flush_buffer()
            self._stats['buffer_flushes'] += 1
    
    def get_stats(self) -> Dict[str, Any]:
        """Get communication statistics"""
        return {
            **self._stats,
            'connected': self._connected,
            'queue_size': len(self._command_queue) if hasattr(self, '_command_queue') else 0
        }
    
    @property
    def is_connected(self) -> bool:
        """Check if serial port is connected"""
        return self._connected and self._serial and self._serial.is_open