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


def discover_procedures() -> list[type[ControlProcedure]]:
    """Return loadable Procedure classes found in the procedures package.

    Procedures with PERSISTENT = True are always loaded as dedicated tabs
    and are excluded here so they don't appear as duplicates in the loader.
    """
    found: list[type[ControlProcedure]] = []
    pkg_path = Path(__file__).parent

    for _finder, name, _ispkg in pkgutil.iter_modules([str(pkg_path)]):
        if name in ("base",) or not name.startswith("proc_"):
            continue
        try:
            mod = importlib.import_module(f".{name}", package=__name__)
            cls = getattr(mod, "Procedure", None)
            if cls is not None and isinstance(cls, type) and issubclass(cls, ControlProcedure):
                if not getattr(cls, "PERSISTENT", False):
                    found.append(cls)
        except Exception as exc:
            print(f"[procedures] Failed to load {name}: {exc}")

    return found
