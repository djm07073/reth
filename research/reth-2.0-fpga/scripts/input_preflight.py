#!/usr/bin/env python3
"""Capture deterministic SC-01 input availability evidence before launch."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from baseline_contract import (
    BLOCKER_ID,
    CORPUS_LOCK_SHA256,
    CORPUS_SHA256,
    ORDERED_HASH_SHA256,
    RETH_COMMIT,
    SNAPSHOT_HEAD,
    SNAPSHOT_IDENTITY,
    canonical_json,
    evidence_digest,
    sha256_file,
)


def git_commit(path: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def corpus_blocks(path: Path) -> int | None:
    if not path.is_file():
        return None
    with path.open("rb") as source:
        return sum(1 for _ in source)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--snapshot-inventory", required=True, type=Path)
    parser.add_argument("--official-response", required=True, type=Path)
    parser.add_argument("--official-http-status", type=int, default=200)
    parser.add_argument("--corpus-permanent-url")
    parser.add_argument("--publication-attempt", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    response = args.official_response.read_bytes()
    response_text = response.decode("utf-8", errors="replace")
    offered = sorted(
        {
            int(value.replace(",", ""))
            for value in re.findall(r"(?:block[^0-9]{0,40})([0-9][0-9,]{6,})", response_text, re.I)
        }
    )
    corpus_present = args.corpus.is_file()
    lock_present = args.lock.is_file()
    snapshot_present = args.snapshot.is_file() and args.snapshot_inventory.is_file()
    corpus_permanent = bool(args.corpus_permanent_url)
    disk = shutil.disk_usage(args.output.parent if args.output.parent.exists() else Path.cwd())

    report = {
        "schema_version": "reth-fpga-input-preflight/v1",
        "blocker_id": BLOCKER_ID,
        "result": "BLOCKED" if not snapshot_present else "READY",
        "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "expected": {
            "reth_commit": RETH_COMMIT,
            "corpus_sha256": CORPUS_SHA256,
            "corpus_lock_sha256": CORPUS_LOCK_SHA256,
            "ordered_block_hashes_sha256": ORDERED_HASH_SHA256,
            "snapshot_logical_identity": SNAPSHOT_IDENTITY,
            "snapshot_head": SNAPSHOT_HEAD,
        },
        "observed": {
            "source": {"path": str(args.source.resolve()), "commit": git_commit(args.source)},
            "corpus": {
                "path": str(args.corpus.resolve()),
                "present": corpus_present,
                "bytes": args.corpus.stat().st_size if corpus_present else None,
                "blocks": corpus_blocks(args.corpus),
                "sha256": sha256_file(args.corpus) if corpus_present else None,
                "lock_path": str(args.lock.resolve()),
                "lock_sha256": sha256_file(args.lock) if lock_present else None,
                "permanent_url": args.corpus_permanent_url,
            },
            "snapshot": {
                "path": str(args.snapshot.resolve()),
                "inventory_path": str(args.snapshot_inventory.resolve()),
                "present": snapshot_present,
                "archive_sha256": sha256_file(args.snapshot) if snapshot_present else None,
                "inventory_sha256": sha256_file(args.snapshot_inventory) if snapshot_present else None,
            },
            "official_snapshot_source": {
                "url": "https://snapshots.reth.rs/",
                "http_status": args.official_http_status,
                "response_sha256": __import__("hashlib").sha256(response).hexdigest(),
                "offered_blocks": offered,
                "exact_snapshot_head_listed": SNAPSHOT_HEAD in offered,
            },
            "host": {
                "system": platform.system(),
                "machine": platform.machine(),
                "release": platform.release(),
            },
            "disk": {
                "available_bytes": disk.free,
                "required_floor_bytes": 531_374_182_400,
                "floor_satisfied": disk.free >= 531_374_182_400,
            },
        },
        "attempted_authorized_local_sources": [
            str(args.snapshot.resolve()),
            str(args.snapshot_inventory.resolve()),
            "https://snapshots.reth.rs/",
            *args.publication_attempt,
        ],
        "missing_required_material": [
            *([] if snapshot_present else ["snapshot_archive_and_inventory"]),
            *([] if corpus_permanent else ["permanent_corpus_artifact"]),
        ],
        "authority": {
            "scope": "local-free-no-credential",
            "aws_or_paid_resources_used": False,
            "credentials_used": False,
        },
        "execution_decision": (
            "launch_primary_trials"
            if snapshot_present and corpus_permanent
            else "do_not_launch_primary_trials"
        ),
    }
    report["evidence_sha256"] = evidence_digest(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(report) + b"\n")
    print(f"{report['result']} evidence_sha256={report['evidence_sha256']}")
    return 0 if snapshot_present else 2


if __name__ == "__main__":
    raise SystemExit(main())
