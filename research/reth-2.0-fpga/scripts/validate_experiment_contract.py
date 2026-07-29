#!/usr/bin/env python3
"""Run the frozen CP-0 experiment contract validator."""

from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "experiment-contract/v1/validate.py"),
    run_name="__main__",
)
