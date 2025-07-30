import asyncio
import time
import logging
from typing import Union, List, Tuple
from crc import append_crc, validate_crc

logger = logging.getLogger(__name__)


class DummyComm:
    """Dummy communication class that simulates telescope mount behavior"""

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600):
        # Simulated mount state
        self._ra_encoder = 0
        self._dec_encoder = 0
        self._ra_target = 0
        self._dec_target = 0
        self._ra_velocity = 0
        self._dec_velocity = 0
        self._ra_acceleration = 1000
        self._dec_acceleration = 1000

        # Movement state
        self._ra_moving = False
        self._dec_moving = False
        self._ra_start_time = 0
        self._dec_start_time = 0
        self._ra_start_pos = 0
        self._dec_start_pos = 0

        # Simulation parameters
        self._max_velocity = 100000  # Maximum velocity in steps/sec
        self._position_noise = 0  # Add noise to position readings (steps)

        # Start simulation task
        self._simulation_task = asyncio.create_task(self._simulate_movement())

        logger.info(f"Dummy telescope mount initialized at RA={self._ra_encoder}, Dec={self._dec_encoder}")

    async def home(self) -> None:
        """Simulate homing command"""
        logger.info("Simulating home command")
        await asyncio.sleep(0.1)  # Simulate command delay

        # Stop any movement
        self._ra_moving = False
        self._dec_moving = False

        # Simulate homing movement
        await asyncio.sleep(2.0)  # Simulate homing time

        # Set to home position (simulate encoder zero)
        self._ra_encoder = 0
        self._dec_encoder = 0

        logger.info("Homing complete - RA=0, Dec=0")

    async def stop(self) -> None:
        """Simulate stop command"""
        logger.info("Simulating stop command")
        await asyncio.sleep(0.05)  # Simulate command delay

        self._ra_moving = False
        self._dec_moving = False
        self._ra_velocity = 0
        self._dec_velocity = 0

        logger.info("All axes stopped")

    async def move_ra_enc(self, ra_enc: int) -> None:
        """Simulate RA movement to encoder position"""
        logger.info(f"Simulating RA move to {ra_enc}")
        await asyncio.sleep(0.05)

        self._ra_target = ra_enc
        self._ra_start_pos = self._ra_encoder
        self._ra_start_time = time.time()
        self._ra_moving = True

    async def move_dec_enc(self, dec_enc: int) -> None:
        """Simulate Dec movement to encoder position"""
        logger.info(f"Simulating Dec move to {dec_enc}")
        await asyncio.sleep(0.05)

        self._dec_target = dec_enc
        self._dec_start_pos = self._dec_encoder
        self._dec_start_time = time.time()
        self._dec_moving = True

    async def move_enc(self, ra_enc: int, dec_enc: int) -> None:
        """Simulate movement to both encoder positions"""
        logger.info(f"Simulating move to RA={ra_enc}, Dec={dec_enc}")
        await asyncio.sleep(0.05)

        # Start both axes
        self._ra_target = ra_enc
        self._dec_target = dec_enc

        current_time = time.time()
        self._ra_start_pos = self._ra_encoder
        self._dec_start_pos = self._dec_encoder
        self._ra_start_time = current_time
        self._dec_start_time = current_time

        self._ra_moving = True
        self._dec_moving = True

    async def get_encoder_positions(self) -> Tuple[int, int]:
        """Return simulated encoder positions"""
        await asyncio.sleep(0.02)  # Simulate communication delay

        # Add small amount of noise to simulate real encoders
        ra_noise = int((hash(time.time()) % 3) - 1) * self._position_noise
        dec_noise = int((hash(time.time() + 1) % 3) - 1) * self._position_noise

        ra_pos = self._ra_encoder + ra_noise
        dec_pos = self._dec_encoder + dec_noise

        return ra_pos, dec_pos

    async def set_velocity(self, ra_vel: int, dec_vel: int) -> None:
        """Set simulated velocity"""
        # Clamp velocities to reasonable limits
        self._ra_velocity = max(-self._max_velocity, min(self._max_velocity, ra_vel))
        self._dec_velocity = max(-self._max_velocity, min(self._max_velocity, dec_vel))

        logger.info(f"Set velocities: RA={self._ra_velocity}, Dec={self._dec_velocity}")
        await asyncio.sleep(0.02)

    async def set_acceleration(self, ra_acc: int, dec_acc: int) -> None:
        """Set simulated acceleration"""
        self._ra_acceleration = max(100, ra_acc)  # Minimum acceleration
        self._dec_acceleration = max(100, dec_acc)

        logger.info(f"Set accelerations: RA={self._ra_acceleration}, Dec={self._dec_acceleration}")
        await asyncio.sleep(0.02)

    async def send_commands(self, commands: Union[str, List[str]]) -> Union[str, List[str]]:
        """Simulate command sending with CRC validation"""
        if isinstance(commands, str):
            commands = [commands]

        response = []
        for cmd in commands:
            # Validate CRC of incoming command
            command_with_crc = append_crc(cmd)
            if not validate_crc(command_with_crc.rstrip('\r')):
                logger.warning(f"Invalid CRC for command: {cmd}")
                response.append("")
                continue

            # Simulate command processing delay
            await asyncio.sleep(0.02)

            # Process command and generate response
            if cmd.startswith("$Status1RA"):
                # Return RA encoder position in expected format
                resp_data = f"RA,{self._ra_encoder}.0"
                resp_with_crc = append_crc(cmd + resp_data)
                response.append(resp_data)

            elif cmd.startswith("$Status1Dec"):
                # Return Dec encoder position in expected format
                resp_data = f"Dec,{self._dec_encoder}.0"
                resp_with_crc = append_crc(cmd + resp_data)
                response.append(resp_data)

            elif cmd.startswith("$StopRA"):
                self._ra_moving = False
                self._ra_velocity = 0
                response.append("OK")

            elif cmd.startswith("$StopDec"):
                self._dec_moving = False
                self._dec_velocity = 0
                response.append("OK")

            elif cmd.startswith("$HomeRA"):
                # Simulate RA homing
                await asyncio.sleep(1.0)
                self._ra_encoder = 0
                self._ra_moving = False
                response.append("OK")

            elif cmd.startswith("$HomeDec"):
                # Simulate Dec homing
                await asyncio.sleep(1.0)
                self._dec_encoder = 0
                self._dec_moving = False
                response.append("OK")

            elif cmd.startswith("$PosRA"):
                # Extract target position
                try:
                    target = int(cmd.split()[1])
                    self._ra_target = target
                    response.append("OK")
                except (IndexError, ValueError):
                    response.append("ERROR")

            elif cmd.startswith("$PosDec"):
                # Extract target position
                try:
                    target = int(cmd.split()[1])
                    self._dec_target = target
                    response.append("OK")
                except (IndexError, ValueError):
                    response.append("ERROR")

            elif cmd.startswith("$RunRA"):
                self._ra_start_pos = self._ra_encoder
                self._ra_start_time = time.time()
                self._ra_moving = True
                response.append("OK")

            elif cmd.startswith("$RunDec"):
                self._dec_start_pos = self._dec_encoder
                self._dec_start_time = time.time()
                self._dec_moving = True
                response.append("OK")

            elif cmd.startswith("$VelRa"):
                try:
                    vel = int(cmd.split()[1])
                    self._ra_velocity = vel
                    response.append("OK")
                except (IndexError, ValueError):
                    response.append("ERROR")

            elif cmd.startswith("$VelDec"):
                try:
                    vel = int(cmd.split()[1])
                    self._dec_velocity = vel
                    response.append("OK")
                except (IndexError, ValueError):
                    response.append("ERROR")

            elif cmd.startswith("$AccelRa"):
                try:
                    acc = int(cmd.split()[1])
                    self._ra_acceleration = acc
                    response.append("OK")
                except (IndexError, ValueError):
                    response.append("ERROR")

            elif cmd.startswith("$AccelDec"):
                try:
                    acc = int(cmd.split()[1])
                    self._dec_acceleration = acc
                    response.append("OK")
                except (IndexError, ValueError):
                    response.append("ERROR")

            else:
                logger.warning(f"Unknown command: {cmd}")
                response.append("UNKNOWN")

        return response[0] if len(response) == 1 else response

    async def _simulate_movement(self):
        """Continuously simulate mount movement"""
        try:
            while True:
                current_time = time.time()

                # Simulate RA movement
                if self._ra_moving:
                    self._update_axis_position('ra', current_time)

                # Simulate Dec movement
                if self._dec_moving:
                    self._update_axis_position('dec', current_time)

                await asyncio.sleep(0.05)  # Update at 20Hz

        except asyncio.CancelledError:
            logger.info("Movement simulation stopped")
        except Exception as e:
            logger.error(f"Movement simulation error: {e}")

    def _update_axis_position(self, axis: str, current_time: float):
        """Update position for a single axis using realistic motion profile"""
        if axis == 'ra':
            moving = self._ra_moving
            target = self._ra_target
            start_pos = self._ra_start_pos
            start_time = self._ra_start_time
            velocity = abs(self._ra_velocity)
            acceleration = self._ra_acceleration
            current_pos = self._ra_encoder
        else:
            moving = self._dec_moving
            target = self._dec_target
            start_pos = self._dec_start_pos
            start_time = self._dec_start_time
            velocity = abs(self._dec_velocity)
            acceleration = self._dec_acceleration
            current_pos = self._dec_encoder

        if not moving:
            return

        dt = current_time - start_time
        distance_to_target = target - start_pos

        # Determine direction
        direction = 1 if distance_to_target >= 0 else -1
        abs_distance = abs(distance_to_target)

        # Simple trapezoidal motion profile
        accel_time = velocity / acceleration
        accel_distance = 0.5 * acceleration * accel_time * accel_time

        if abs_distance <= 2 * accel_distance:
            # Triangular profile (no constant velocity phase)
            max_vel = (acceleration * abs_distance / 2) ** 0.5
            accel_time = max_vel / acceleration

            if dt <= accel_time:
                # Accelerating
                pos_offset = 0.5 * acceleration * dt * dt
            else:
                # Decelerating
                decel_dt = dt - accel_time
                pos_offset = accel_distance + max_vel * decel_dt - 0.5 * acceleration * decel_dt * decel_dt
        else:
            # Trapezoidal profile
            const_distance = abs_distance - 2 * accel_distance
            const_time = const_distance / velocity
            total_accel_time = accel_time
            total_const_time = const_time
            total_decel_time = total_accel_time

            if dt <= total_accel_time:
                # Accelerating
                pos_offset = 0.5 * acceleration * dt * dt
            elif dt <= total_accel_time + total_const_time:
                # Constant velocity
                const_dt = dt - total_accel_time
                pos_offset = accel_distance + velocity * const_dt
            else:
                # Decelerating
                decel_dt = dt - total_accel_time - total_const_time
                pos_offset = accel_distance + velocity * total_const_time + velocity * decel_dt - 0.5 * acceleration * decel_dt * decel_dt

        # Calculate new position
        new_pos = start_pos + direction * pos_offset

        # Check if we've reached the target
        if direction > 0 and new_pos >= target:
            new_pos = target
            if axis == 'ra':
                self._ra_moving = False
            else:
                self._dec_moving = False
        elif direction < 0 and new_pos <= target:
            new_pos = target
            if axis == 'ra':
                self._ra_moving = False
            else:
                self._dec_moving = False

        # Update position
        if axis == 'ra':
            self._ra_encoder = int(new_pos)
        else:
            self._dec_encoder = int(new_pos)

    def set_position_noise(self, noise_steps: int):
        """Set amount of noise to add to position readings"""
        self._position_noise = noise_steps
        logger.info(f"Position noise set to {noise_steps} steps")

    def get_simulated_state(self) -> dict:
        """Get internal simulation state for debugging"""
        return {
            'ra_encoder': self._ra_encoder,
            'dec_encoder': self._dec_encoder,
            'ra_target': self._ra_target,
            'dec_target': self._dec_target,
            'ra_velocity': self._ra_velocity,
            'dec_velocity': self._dec_velocity,
            'ra_moving': self._ra_moving,
            'dec_moving': self._dec_moving
        }

    async def cleanup(self):
        """Clean up simulation task"""
        if self._simulation_task and not self._simulation_task.done():
            self._simulation_task.cancel()
            try:
                await self._simulation_task
            except asyncio.CancelledError:
                pass
        logger.info("Dummy comm cleaned up")