# Copyright 2026 Nutanix. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Finding the scenarios.

Everything in the top-level ``scenarios/`` directory is a scenario. Drop a
``.yaml`` file there and it is picked up - there is no registry to edit and no
Python to write.

A ``.py`` file in the same directory also works, for the rare case that needs
real logic. It must subclass ``Scenario``. Prefer YAML: it is what the rest of
the team will read.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys

from pathlib import Path

from .core import Scenario
from .spec import discover_specs

#: scenarios/ sits beside this package, not inside it
SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def _load_python(path: Path) -> dict[str, type[Scenario]]:
    spec = importlib.util.spec_from_file_location(f"_scenario_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import scenario {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    found = {}
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Scenario) and obj is not Scenario and obj.name:
            obj.source = str(path)
            found[obj.name] = obj
    return found


def discover(directory: Path | None = None) -> dict[str, type[Scenario]]:
    """Return every scenario in ``scenarios/``, keyed by name."""
    directory = directory or SCENARIO_DIR
    if not directory.is_dir():
        raise RuntimeError(f"no scenarios directory at {directory}")

    found: dict[str, type[Scenario]] = dict(discover_specs(directory))
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        for name, cls in _load_python(path).items():
            if name in found:
                raise RuntimeError(
                    f"scenario '{name}' is defined twice: {found[name].source} and {path}"
                )
            found[name] = cls
    return dict(sorted(found.items()))
