#!/usr/bin/env python3
"""Validate the immutable 664-block SC-01 corpus with practical defaults."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[4]
CORPUS_DIR = HERE.parents[1]
contract = ROOT / "research/reth-2.0-fpga/experiment-contract/v1/manifest.json"
corpus = Path(os.environ.get("RETH_FPGA_CORPUS", ROOT / "artifacts/oh-96/inputs/corpus.jsonl"))
lock = Path(os.environ.get("RETH_FPGA_CORPUS_LOCK", ROOT / "artifacts/oh-96/inputs/corpus-lock.json"))
source = Path(os.environ.get("RETH_V2_SOURCE", "/private/tmp/oh37-reth-v2.xtMNvP"))

raise SystemExit(
    subprocess.run(
        [
            sys.executable,
            str(CORPUS_DIR / "validate.py"),
            "--contract",
            str(contract),
            "--corpus",
            str(corpus),
            "--lock",
            str(lock),
            "--reth-source",
            str(source),
        ]
    ).returncode
)
