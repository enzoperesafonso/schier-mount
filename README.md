# Schier Mount Control

A Python package to control the Schier Mount of ROTSE-IIIc.

## Installation

```bash
poetry install
```

## Usage

### Interactive CLI

You can run the interactive CLI tool to control the mount:

```bash
poetry run schier-mount-interactive
```

### Library Usage

```python
from schier_mount.schier import SchierMount
import asyncio

async def main():
    mount = SchierMount()
    await mount.init_mount()
    # ... your code here ...

if __name__ == "__main__":
    asyncio.run(main())
```

## Project Structure

- `src/schier_mount/`: Main package source.
  - `comm.py`: Low-level communication with the mount.
  - `configuration.py`: Mount configuration settings.
  - `coordinates.py`: Coordinate transformations.
  - `schier.py`: High-level mount control logic.
  - `interactive.py`: Interactive CLI tool.
  - `crc.py`: CRC calculation utilities.
