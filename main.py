#!/usr/bin/env python3
"""Project-root launcher for the dashboard/bot CLI.

Allows running:
    python main.py --dashboard --update-interval 1.0
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    venv_python = root / ".venv" / "bin" / "python"
    in_project_venv = False
    try:
        in_project_venv = Path(sys.prefix).resolve() == (root / ".venv").resolve()
    except Exception:
        in_project_venv = False

    # Ensure runtime uses project venv even when shell default Python is system-wide.
    if (
        venv_python.exists()
        and (not in_project_venv)
        and os.environ.get("AITRADER_VENV_BOOTSTRAPPED", "") != "1"
    ):
        env = dict(os.environ)
        env["AITRADER_VENV_BOOTSTRAPPED"] = "1"
        os.execve(
            str(venv_python),
            [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
            env,
        )

    src_dir = root / "src"
    src_main = src_dir / "main.py"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    runpy.run_path(str(src_main), run_name="__main__")
