#!/usr/bin/env python3
"""Re-derive the OH-155 full-workload phase accounting from retained raw evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from pathlib import Path


def load_prometheus(path: Path) -> dict[str, float]:
    result: dict[str, float] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            try:
                result[fields[0]] = float(fields[1])
            except ValueError:
                pass
    return result


def delta(before: dict[str, float], after: dict[str, float], base: str) -> float:
    wanted = base + "_sum"
    return max(0.0, sum(
        value - before.get(name, 0.0)
        for name, value in after.items()
        if name.split("{", 1)[0] == wanted
    ))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    trials = []
    for number in range(1, 4):
        root = args.baseline / f"trial-{number}"
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        before = load_prometheus(root / "metrics-before.prom")
        after = load_prometheus(root / "metrics-after.prom")
        with (root / "measured" / "combined_latency.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != 500:
            raise RuntimeError(f"trial {number}: expected 500 per-block rows, got {len(rows)}")
        trials.append({
            "trial": number,
            "valid": manifest["valid"],
            "manifest_sha256": sha256(manifest_path),
            "wall_seconds": manifest["e2e_wall_clock_seconds"],
            "persistence_wait_seconds": sum(int(row["persistence_wait"]) for row in rows) / 1e6,
            "persistence_waits_over_one_second": sum(int(row["persistence_wait"]) > 1_000_000 for row in rows),
            "execution_seconds": delta(before, after, "reth_sync_execution_execution_histogram"),
            "state_root_seconds": delta(before, after, "reth_sync_block_validation_state_root_histogram"),
            "trie_cursor_seconds": delta(before, after, "reth_trie_cursor_overall_duration"),
            "hashed_cursor_seconds": delta(before, after, "reth_trie_hashed_cursor_overall_duration"),
            "save_blocks_mdbx_seconds": delta(before, after, "reth_storage_providers_database_save_blocks_mdbx"),
            "write_trie_updates_seconds": delta(before, after, "reth_storage_providers_database_save_blocks_write_trie_updates"),
            "write_hashed_state_seconds": delta(before, after, "reth_storage_providers_database_save_blocks_write_hashed_state"),
            "commit_whole_seconds": delta(before, after, "reth_database_transaction_commit_whole_duration_seconds"),
            "commit_sync_seconds": delta(before, after, "reth_database_transaction_commit_sync_duration_seconds"),
        })

    output = {
        "schema_version": "reth-fpga-oh155-baseline-reduction/v1",
        "source": str(args.baseline),
        "trials": trials,
        "median_wall_seconds": statistics.median(row["wall_seconds"] for row in trials),
        "all_trials_valid": all(row["valid"] for row in trials),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "median_wall_seconds": output["median_wall_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
