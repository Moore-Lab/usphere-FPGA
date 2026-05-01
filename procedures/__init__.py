"""
Procedure discovery for usphere-control automation procedures.

Each .py file in this directory whose name starts with 'proc_' is scanned
for a ``Procedure`` class that subclasses ``ControlProcedure``.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path

from .base import ControlProcedure


def _scan_procedures() -> list[type[ControlProcedure]]:
    """Return all Procedure classes found in the procedures package, in file order."""
    found: list[type[ControlProcedure]] = []
    pkg_path = Path(__file__).parent
    for _finder, name, _ispkg in pkgutil.iter_modules([str(pkg_path)]):
        if name in ("base",) or not name.startswith("proc_"):
            continue
        try:
            mod = importlib.import_module(f".{name}", package=__name__)
            cls = getattr(mod, "Procedure", None)
            if cls is not None and isinstance(cls, type) and issubclass(cls, ControlProcedure):
                found.append(cls)
        except Exception as exc:
            print(f"[procedures] Failed to load {name}: {exc}")
    return found


def discover_procedures() -> list[type[ControlProcedure]]:
    """Return non-persistent Procedure classes (shown in the legacy Procedures loader)."""
    return [cls for cls in _scan_procedures() if not getattr(cls, "PERSISTENT", False)]


def discover_all_procedures() -> list[type[ControlProcedure]]:
    """Return all Procedure classes, including PERSISTENT ones (used by ResourcesWidget)."""
    return _scan_procedures()
