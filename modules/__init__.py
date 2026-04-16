"""
Hardware module discovery for usphere-control.

Each mod_*.py file in this directory is scanned for the required protocol
attributes (MODULE_NAME, DEVICE_NAME, CONFIG_FIELDS, DEFAULTS).
See modules/base.py for the full protocol specification.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def discover_hardware_modules() -> list:
    """
    Import and return all hardware module objects found in this directory.

    A file is included if it matches mod_*.py and exposes MODULE_NAME,
    DEVICE_NAME, CONFIG_FIELDS, and DEFAULTS at module level.
    Modules that fail to import are skipped with a printed warning.
    """
    found = []
    modules_dir = Path(__file__).parent

    for path in sorted(modules_dir.glob("mod_*.py")):
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if all(hasattr(mod, attr) for attr in
                   ("MODULE_NAME", "DEVICE_NAME", "CONFIG_FIELDS", "DEFAULTS")):
                found.append(mod)
            else:
                print(f"[modules] {path.name}: missing required protocol attributes — skipped")
        except Exception as exc:
            print(f"[modules] Failed to load {path.name}: {exc}")

    return found
