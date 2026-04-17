"""
Run once to create a Windows desktop shortcut for fpga_gui.py.

    python create_shortcut.py

Requires: pywin32  (pip install pywin32)
"""

import os
import sys
from pathlib import Path

try:
    from win32com.client import Dispatch
except ImportError:
    print("pywin32 is required.  Install it with:\n  pip install pywin32")
    sys.exit(1)

PROJECT_DIR   = Path(__file__).resolve().parent
SCRIPT        = PROJECT_DIR / "fpga_gui.py"
ICON          = PROJECT_DIR / "assets" / "uCTRL_logo.ico"
# Ask the Windows shell for the real Desktop path (handles OneDrive redirection)
_shell   = Dispatch("WScript.Shell")
DESKTOP  = Path(_shell.SpecialFolders("Desktop"))
SHORTCUT_PATH = DESKTOP / "usphere CTRL.lnk"

# Prefer pythonw.exe (no console window); fall back to python.exe
pythonw    = Path(sys.executable).parent / "pythonw.exe"
python_exe = str(pythonw if pythonw.exists() else sys.executable)

shell    = _shell
shortcut = shell.CreateShortCut(str(SHORTCUT_PATH))
shortcut.TargetPath      = python_exe
shortcut.Arguments       = f'"{SCRIPT}"'
shortcut.WorkingDirectory = str(PROJECT_DIR)
shortcut.Description     = "usphere FPGA Control"
shortcut.IconLocation    = str(ICON) if ICON.exists() else python_exe
shortcut.save()

print(f"Shortcut created: {SHORTCUT_PATH}")
