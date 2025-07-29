import asyncio
from typing import Union, List, Tuple
from aioserial import AioSerial

from src.schier_mount.utils.crc import append_crc, validate_crc

class Comm:
    """Low-level mount communication interface"""
    def __init__(self, device: str = "/dev/ttyS0", baudrate: int = 9600):
        self._aioserial = AioSerial(device, baudrate=baudrate)

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
        split = [r.split(",") for r in resp]
        return int(float(split[0][1])), int(float(split[1][1]))

    async def set_velocity(self, ra_vel: int, dec_vel: int) -> None:
        """Set the velocity as steps/s."""
        await self.send_commands([f"$VelRa {ra_vel:06d}", f"$VelDec {dec_vel:06d}"])

    async def set_acceleration(self, ra_acc: int, dec_acc: int) -> None:
        """Set the acceleration as steps/s²."""
        await self.send_commands([f"$AccelRa {ra_acc:06d}", f"$AccelDec {dec_acc:06d}"])

    async def send_commands(self, commands: Union[str, List[str]]) -> Union[str, List[str]]:
        """Sends the command with CRC over the serial port."""
        if isinstance(commands, str):
            commands = [commands]

        response = []
        for cmd in commands:
            command_with_crc = append_crc(cmd)

            # Send the encoded command
            await self._aioserial.write_async(command_with_crc.encode())
            await asyncio.sleep(0.1)

            # get response
            res = (await self._aioserial.read_until_async(b"\r")).decode().strip()  # Read the response
            if validate_crc(res):
                response.append(res[len(cmd) : -4].strip())
            else:
                response.append("")

        return response[0] if len(response) == 1 else response
