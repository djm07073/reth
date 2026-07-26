#!/usr/bin/env python3
"""Validate a CP-1 corpus and its exact content lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from corpus import CorpusError, validate_lock, verify_reth_source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument(
        "--reth-source",
        type=Path,
        default=os.environ.get("RETH_V2_SOURCE"),
        required="RETH_V2_SOURCE" not in os.environ,
    )
    args = parser.parse_args()
    try:
        verify_reth_source(args.reth_source)
        lock, _ = validate_lock(args.contract, args.corpus, args.lock)
    except CorpusError as error:
        parser.error(str(error))
    print(
        f"OK {lock['canonical_identity']}: {lock['range']['blocks']} blocks, "
        f"{lock['artifact']['bytes']} bytes, sha256={lock['artifact']['artifact_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
