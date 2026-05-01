"""
fpga/

NI PXIe-7856R FPGA driver package.

This directory contains all hardware-specific code for the FPGA and is
designed to be extracted as a standalone git submodule in the future.
Nothing outside this package should import fpga.* directly — callers
should use the public re-exports below or go through the FPGAFacade
interface defined in procedures/base.py.

Sub-modules
-----------
fpga.core       — FPGAController: session management, register I/O, polling
fpga.registers  — Register definitions, coefficient helpers
fpga.plot       — FPGAPlotWidget: real-time 3x3 pyqtgraph plot grid
fpga.ipc        — TICPublisher, ShakeEventLogger: file-based IPC to usphere-DAQ
"""

from fpga.core import FPGAConfig, FPGAController
from fpga.registers import REGISTER_MAP, REGISTERS, RegisterDef
from fpga.plot import ALL_PLOT_NAMES, FPGAPlotWidget
from fpga.ipc import ShakeEventLogger, TICPublisher

__all__ = [
    "FPGAConfig",
    "FPGAController",
    "REGISTER_MAP",
    "REGISTERS",
    "RegisterDef",
    "ALL_PLOT_NAMES",
    "FPGAPlotWidget",
    "ShakeEventLogger",
    "TICPublisher",
]
