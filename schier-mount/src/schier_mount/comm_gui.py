import sys
import asyncio
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QLabel,
    QLineEdit, QHBoxLayout, QTextEdit
)
from asyncqt import QEventLoop
from telescope_comm import Comm  # <-- adjust if Comm is in a different file

class TelescopeControlApp(QWidget):
    def __init__(self, comm: Comm):
        super().__init__()
        self.comm = comm
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Telescope Controller")

        layout = QVBoxLayout()

        # Encoder input fields
        self.ra_input = QLineEdit()
        self.ra_input.setPlaceholderText("RA Encoder Value")
        self.dec_input = QLineEdit()
        self.dec_input.setPlaceholderText("Dec Encoder Value")

        move_buttons = QHBoxLayout()
        self.move_ra_btn = QPushButton("Move RA")
        self.move_dec_btn = QPushButton("Move Dec")
        self.move_both_btn = QPushButton("Move RA + Dec")
        move_buttons.addWidget(self.move_ra_btn)
        move_buttons.addWidget(self.move_dec_btn)
        move_buttons.addWidget(self.move_both_btn)

        self.home_btn = QPushButton("Home Telescope")
        self.stop_btn = QPushButton("Stop Telescope")
        self.get_pos_btn = QPushButton("Get Encoder Positions")

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        layout.addWidget(QLabel("RA Encoder:"))
        layout.addWidget(self.ra_input)
        layout.addWidget(QLabel("Dec Encoder:"))
        layout.addWidget(self.dec_input)
        layout.addLayout(move_buttons)
        layout.addWidget(self.home_btn)
        layout.addWidget(self.stop_btn)
        layout.addWidget(self.get_pos_btn)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log)

        self.setLayout(layout)
        self.bind_events()

    def bind_events(self):
        self.home_btn.clicked.connect(lambda: asyncio.ensure_future(self.run_and_log(self.comm.home())))
        self.stop_btn.clicked.connect(lambda: asyncio.ensure_future(self.run_and_log(self.comm.stop())))
        self.move_ra_btn.clicked.connect(lambda: asyncio.ensure_future(self.move_ra()))
        self.move_dec_btn.clicked.connect(lambda: asyncio.ensure_future(self.move_dec()))
        self.move_both_btn.clicked.connect(lambda: asyncio.ensure_future(self.move_both()))
        self.get_pos_btn.clicked.connect(lambda: asyncio.ensure_future(self.get_positions()))

    async def run_and_log(self, coro):
        try:
            result = await coro
            if result:
                self.log.append(str(result))
        except Exception as e:
            self.log.append(f"Error: {e}")

    async def move_ra(self):
        try:
            ra = int(self.ra_input.text())
            await self.comm.move_ra_enc(ra)
            self.log.append(f"Moved RA to {ra}")
        except Exception as e:
            self.log.append(f"Invalid RA value: {e}")

    async def move_dec(self):
        try:
            dec = int(self.dec_input.text())
            await self.comm.move_dec_enc(dec)
            self.log.append(f"Moved Dec to {dec}")
        except Exception as e:
            self.log.append(f"Invalid Dec value: {e}")

    async def move_both(self):
        try:
            ra = int(self.ra_input.text())
            dec = int(self.dec_input.text())
            await self.comm.move_enc(ra, dec)
            self.log.append(f"Moved RA to {ra}, Dec to {dec}")
        except Exception as e:
            self.log.append(f"Invalid encoder values: {e}")

    async def get_positions(self):
        try:
            ra, dec = await self.comm.get_encoder_positions()
            self.log.append(f"RA: {ra}, Dec: {dec}")
        except Exception as e:
            self.log.append(f"Failed to get positions: {e}")


def main():
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    comm = Comm("/dev/ttyS0", baudrate=9600)

    window = TelescopeControlApp(comm)
    window.resize(400, 500)
    window.show()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
