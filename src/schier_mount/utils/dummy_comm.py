import asyncio
import time
from typing import Union, List, Tuple
from enum import Enum


class AxisState(Enum):
    HALT = "halt"
    STOP = "stop"
    RUNNING = "running"


class DummyComm:
    """Simulated telescope communication interface for testing"""

    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600):
        # Simulate connection parameters (not actually used)
        self.device = device
        self.baudrate = baudrate

        # Telescope state
        self.ra_encoder_pos = 0
        self.dec_encoder_pos = 0
        self.ra_target_pos = 0
        self.dec_target_pos = 0

        # Motion parameters
        self.ra_velocity = 1000  # steps/sec
        self.dec_velocity = 1000  # steps/sec
        self.ra_acceleration = 5000  # steps/sec^2
        self.dec_acceleration = 5000  # steps/sec^2
        self.ra_max_velocity = 50000
        self.dec_max_velocity = 50000

        # Axis states
        self.ra_state = AxisState.HALT
        self.dec_state = AxisState.HALT

        # Limits (encoder positions)
        self.ra_positive_limit = 1000000
        self.ra_negative_limit = -1000000
        self.dec_positive_limit = 500000
        self.dec_negative_limit = -500000

        # Motion simulation
        self.ra_last_update = time.time()
        self.dec_last_update = time.time()
        self.ra_current_velocity = 0
        self.dec_current_velocity = 0

        # Servo parameters
        self.ra_p_gain = 100
        self.ra_i_gain = 10
        self.ra_d_gain = 5
        self.dec_p_gain = 100
        self.dec_i_gain = 10
        self.dec_d_gain = 5

        # Fault tracking
        self.recent_faults = []

        # Motion simulation task (will be started on first use)
        self._motion_task = None
        self._simulation_started = False

    async def _ensure_motion_simulation(self):
        """Ensure the motion simulation task is running"""
        if not self._simulation_started or (self._motion_task and self._motion_task.done()):
            self._motion_task = asyncio.create_task(self._simulate_motion())
            self._simulation_started = True

    def _start_motion_simulation(self):
        """Legacy method - no longer used"""
        pass

    async def _simulate_motion(self):
        """Background task to simulate telescope motion"""
        while True:
            await asyncio.sleep(0.1)  # Update every 100ms
            current_time = time.time()

            # Update RA position
            if self.ra_state == AxisState.RUNNING:
                dt = current_time - self.ra_last_update

                # Calculate direction and distance to target
                distance_to_target = self.ra_target_pos - self.ra_encoder_pos

                if abs(distance_to_target) > 1:  # Not at target yet
                    # Determine direction
                    direction = 1 if distance_to_target > 0 else -1

                    # Simple motion: move at constant velocity toward target
                    step = direction * self.ra_velocity * dt

                    # Don't overshoot
                    if abs(step) > abs(distance_to_target):
                        step = distance_to_target

                    new_pos = self.ra_encoder_pos + step

                    # Check limits
                    if new_pos > self.ra_positive_limit:
                        new_pos = self.ra_positive_limit
                        self._add_fault("Axis 1 Positive Limit")
                        self.ra_state = AxisState.HALT
                        self.ra_current_velocity = 0  # Stop immediately
                    elif new_pos < self.ra_negative_limit:
                        new_pos = self.ra_negative_limit
                        self._add_fault("Axis 1 Negative Limit")
                        self.ra_state = AxisState.HALT
                        self.ra_current_velocity = 0  # Stop immediately

                    self.ra_encoder_pos = int(new_pos)
                else:
                    # Reached target, stop
                    self.ra_state = AxisState.STOP

            self.ra_last_update = current_time

            # Update Dec position (similar logic)
            if self.dec_state == AxisState.RUNNING:
                dt = current_time - self.dec_last_update

                distance_to_target = self.dec_target_pos - self.dec_encoder_pos

                if abs(distance_to_target) > 1:
                    direction = 1 if distance_to_target > 0 else -1
                    step = direction * self.dec_velocity * dt

                    if abs(step) > abs(distance_to_target):
                        step = distance_to_target

                    new_pos = self.dec_encoder_pos + step

                    # Check limits
                    if new_pos > self.dec_positive_limit:
                        new_pos = self.dec_positive_limit
                        self._add_fault("Axis 2 Positive Limit")
                        self.dec_state = AxisState.HALT
                        self.dec_current_velocity = 0  # Stop immediately
                    elif new_pos < self.dec_negative_limit:
                        new_pos = self.dec_negative_limit
                        self._add_fault("Axis 2 Negative Limit")
                        self.dec_state = AxisState.HALT
                        self.dec_current_velocity = 0  # Stop immediately

                    self.dec_encoder_pos = int(new_pos)
                else:
                    self.dec_state = AxisState.STOP

            self.dec_last_update = current_time

    def _add_fault(self, fault_type: str):
        """Add a fault to the recent faults list"""
        timestamp = time.strftime("%H:%M:%S %m/%d/%Y")
        fault_entry = f"{fault_type}, {timestamp}"
        self.recent_faults.insert(0, fault_entry)
        # Keep only last 9 faults
        if len(self.recent_faults) > 9:
            self.recent_faults = self.recent_faults[:9]

    async def home(self) -> None:
        """Send home command to telescope"""
        await self.send_commands(["$StopRA", "$StopDec", "$HomeRA", "$HomeDec"])

    async def stop(self) -> None:
        """Send the stop command to the telescope"""
        await self.send_commands(["$StopRA", "$StopDec"])

    async def move_ra_enc(self, ra_enc: int) -> None:
        """Move to given encoder positions."""
        await self.send_commands(["$StopRA", f"$PosRA {ra_enc}", "$RunRA"])

    async def move_dec_enc(self, dec_enc: int) -> None:
        """Move to given encoder positions."""
        await self.send_commands(["$StopDec", f"$PosDec {dec_enc}", "$RunDec"])

    async def move_enc(self, ra_enc: int, dec_enc: int) -> None:
        """Move to given encoder positions."""
        await self.send_commands(["$StopRA", "$StopDec", f"$PosRA {ra_enc}", f"$PosDec {dec_enc}", "$RunRA", "$RunDec"])

    async def get_encoder_positions(self) -> Tuple[int, int]:
        """Return encoder positions for RA and Dec."""
        resp = await self.send_commands(["$Status1RA", "$Status1Dec"])
        # Parse the simulated response format
        ra_parts = resp[0].split(",")
        dec_parts = resp[1].split(",")
        return int(float(ra_parts[1].strip())), int(float(dec_parts[1].strip()))

    async def set_velocity(self, ra_vel: int, dec_vel: int) -> None:
        """Set the velocity as steps/s."""
        await self.send_commands([f"$VelRa {ra_vel:06d}", f"$VelDec {dec_vel:06d}"])

    async def set_acceleration(self, ra_acc: int, dec_acc: int) -> None:
        """Set the acceleration as steps/s²."""
        await self.send_commands([f"$AccelRa {ra_acc:06d}", f"$AccelDec {dec_acc:06d}"])

    async def set_track_sidereal(self, sidereal_encoder_rate=-1000):
        await self.send_commands(
            ["$StopRA", "$StopDec", f"$VelRa {sidereal_encoder_rate}", f"$VelDec {0}", "$RunRA", "$RunDec"])

    async def send_commands(self, commands: Union[str, List[str]]) -> Union[str, List[str]]:
        """Simulate sending commands and return appropriate responses"""
        # Ensure motion simulation is running
        await self._ensure_motion_simulation()

        if isinstance(commands, str):
            commands = [commands]

        response = []
        for cmd in commands:
            # Simulate small delay
            await asyncio.sleep(0.01)

            # Parse and simulate command
            resp = await self._process_command(cmd.strip())
            response.append(resp)

        return response[0] if len(response) == 1 else response

    async def _process_command(self, cmd: str) -> str:
        """Process individual commands and return simulated responses"""
        cmd = cmd.strip()

        if cmd == "$StopRA":
            self.ra_state = AxisState.STOP
            return ""
        elif cmd == "$StopDec":
            self.dec_state = AxisState.STOP
            return ""
        elif cmd == "$HaltRA":
            self.ra_state = AxisState.HALT
            return ""
        elif cmd == "$HaltDec":
            self.dec_state = AxisState.HALT
            return ""
        elif cmd == "$HomeRA":
            self.ra_target_pos = 0
            self.ra_state = AxisState.RUNNING
            return ""
        elif cmd == "$HomeDec":
            self.dec_target_pos = 0
            self.dec_state = AxisState.RUNNING
            return ""
        elif cmd == "$RunRA":
            if self.ra_state != AxisState.HALT:
                self.ra_state = AxisState.RUNNING
            return ""
        elif cmd == "$RunDec":
            if self.dec_state != AxisState.HALT:
                self.dec_state = AxisState.RUNNING
            return ""
        elif cmd.startswith("$PosRA "):
            pos = int(cmd.split()[1])
            self.ra_target_pos = pos
            return f" {pos}"
        elif cmd.startswith("$PosDec "):
            pos = int(cmd.split()[1])
            self.dec_target_pos = pos
            return f" {pos}"
        elif cmd.startswith("$VelRa "):
            vel = int(cmd.split()[1])
            self.ra_velocity = abs(vel)  # Store as positive, direction handled elsewhere
            return f" {vel:06d}"
        elif cmd.startswith("$VelDec "):
            vel = int(cmd.split()[1])
            self.dec_velocity = abs(vel)
            return f" {vel:06d}"
        elif cmd.startswith("$AccelRa "):
            acc = int(cmd.split()[1])
            self.ra_acceleration = acc
            return f" {acc:06d}"
        elif cmd.startswith("$AccelDec "):
            acc = int(cmd.split()[1])
            self.dec_acceleration = acc
            return f" {acc:06d}"
        elif cmd == "$Status1RA":
            return f" {self.ra_target_pos}, {self.ra_encoder_pos}"
        elif cmd == "$Status1Dec":
            return f" {self.dec_target_pos}, {self.dec_encoder_pos}"
        elif cmd == "$Status2RA":
            # Generate status word (16-bit hex)
            status = 0
            if self.ra_state == AxisState.HALT:
                status |= 0x03  # Brake engaged + amplifier disabled
            return f" {status:04X}"
        elif cmd == "$Status2Dec":
            status = 0
            if self.dec_state == AxisState.HALT:
                status |= 0x03
            return f" {status:04X}"
        elif cmd == "$Status3RA":
            # Simulate amplifier drive signal and integrator value
            return f" 0, 0"
        elif cmd == "$Status3Dec":
            return f" 0, 0"
        elif cmd == "$RecentFaults":
            if self.recent_faults:
                return " " + "; ".join(self.recent_faults)
            else:
                return " No recent faults"
        else:
            # Unknown command
            return "?"

    async def __aenter__(self):
        """Async context manager entry"""
        await self._ensure_motion_simulation()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self._motion_task and not self._motion_task.done():
            self._motion_task.cancel()
            try:
                await self._motion_task
            except asyncio.CancelledError:
                pass

    def __del__(self):
        """Clean up motion simulation task"""
        if hasattr(self, '_motion_task') and self._motion_task and not self._motion_task.done():
            self._motion_task.cancel()


# Example usage and testing
async def test_dummy_comm():
    """Test the dummy communication interface"""
    telescope = DummyComm()

    print("Initial position:", await telescope.get_encoder_positions())

    # Test movement
    await telescope.set_velocity(5000, 3000)
    await telescope.move_enc(10000, 5000)

    # Wait a bit and check position
    await asyncio.sleep(1)
    print("Position after 1s:", await telescope.get_encoder_positions())

    await asyncio.sleep(2)
    print("Position after 3s:", await telescope.get_encoder_positions())

    # Stop motion
    await telescope.stop()
    print("Final position:", await telescope.get_encoder_positions())

    # Test homing
    await telescope.home()
    await asyncio.sleep(3)
    print("Position after homing:", await telescope.get_encoder_positions())


if __name__ == "__main__":
    asyncio.run(test_dummy_comm())