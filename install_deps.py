"""
install_deps.py

Create a virtual environment and install dependencies for usphere-FPGA.

Usage:
    python install_deps.py            # Create .venv and install
    python install_deps.py --no-venv  # Install into current environment
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REQUIREMENTS = Path(__file__).parent / "requirements.txt"
VENV_DIR = Path(__file__).parent / ".venv"


def main() -> None:
    use_venv = "--no-venv" not in sys.argv

    if use_venv:
        if not VENV_DIR.exists():
            print(f"Creating virtual environment in {VENV_DIR} ...")
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        pip = str(VENV_DIR / "Scripts" / "pip.exe") if os.name == "nt" else str(VENV_DIR / "bin" / "pip")
    else:
        pip = "pip"

    print("Upgrading pip ...")
    subprocess.check_call([pip, "install", "--upgrade", "pip"])

    print(f"Installing from {REQUIREMENTS} ...")
    subprocess.check_call([pip, "install", "-r", str(REQUIREMENTS)])

    print("\nDone. Activate the environment and launch the GUI:")
    if os.name == "nt":
        print(f"  {VENV_DIR}\\Scripts\\activate")
    else:
        print(f"  source {VENV_DIR}/bin/activate")
    print("  python fpga_gui.py")


if __name__ == "__main__":
    main()
