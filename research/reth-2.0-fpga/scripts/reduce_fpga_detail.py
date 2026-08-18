#!/usr/bin/env python3
"""Reduce detailed FPGA-target instrumentation from two Prometheus boundary scrapes."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SAMPLE = re.compile(r'^([^\s{]+)(?:\{(.*)\})?\s+([-+0-9.eE]+)$')
LABEL = re.compile(r'(\w+)="((?:\\.|[^"])*)"')


def parse(path: Path) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    result = {}
    for line in path.read_text().splitlines():
        match = SAMPLE.match(line)
        if not match:
            continue
        name, raw_labels, value = match.groups()
        labels = tuple(sorted((key, bytes(val, "utf8").decode("unicode_escape")) for key, val in LABEL.findall(raw_labels or "")))
        result[(name, labels)] = float(value)
    return result


def delta(
    before: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    after: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    required_labels: dict[str, str] | None = None,
) -> float:
    total = 0.0
    required_labels = required_labels or {}
    for (metric, labels), value in after.items():
        label_map = dict(labels)
        if metric == name and all(label_map.get(key) == val for key, val in required_labels.items()):
            total += value - before.get((metric, labels), 0.0)
    return max(0.0, total)


def metric_value(
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float], name: str
) -> float | None:
    values = [value for (metric, _), value in samples.items() if metric == name]
    return values[0] if len(values) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--samply-analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    before = parse(args.before)
    after = parse(args.after)
    manifest = json.loads(args.manifest.read_text())
    samply = json.loads(args.samply_analysis.read_text())

    operation_labels: set[tuple[str, str]] = set()
    for (name, labels), value in after.items():
        label_map = dict(labels)
        if name == "reth_database_operation_calls_total" and value > before.get((name, labels), 0):
            operation_labels.add((label_map.get("table", ""), label_map.get("operation", "")))

    operations: list[dict[str, Any]] = []
    for table, operation in sorted(operation_labels):
        labels = {"table": table, "operation": operation}
        calls = delta(before, after, "reth_database_operation_calls_total", labels)
        duration = delta(before, after, "reth_database_operation_duration_seconds_sum", labels)
        logical_bytes = delta(before, after, "reth_database_operation_logical_bytes_total", labels)
        result_bytes = delta(before, after, "reth_database_operation_result_bytes_total", labels)
        serialization = delta(
            before,
            after,
            "reth_database_operation_serialization_duration_seconds_sum",
            labels,
        )
        serialized_bytes = delta(
            before, after, "reth_database_operation_serialized_bytes_total", labels
        )
        operations.append(
            {
                "table": table,
                "operation": operation,
                "calls": int(round(calls)),
                "duration_seconds": duration,
                "average_duration_ns": duration / calls * 1e9 if calls else 0.0,
                "logical_input_or_deleted_bytes": int(round(logical_bytes)),
                "result_bytes": int(round(result_bytes)),
                "average_logical_bytes": logical_bytes / calls if calls else 0.0,
                "serialization_seconds": serialization,
                "serialized_bytes": int(round(serialized_bytes)),
                "average_serialization_ns": serialization / calls * 1e9 if calls else 0.0,
            }
        )

    operations.sort(key=lambda row: row["duration_seconds"], reverse=True)
    targeted = [
        row
        for row in operations
        if any(token in row["operation"] for token in ("seek", "delete", "upsert"))
    ]
    trie_operations = [
        row for row in targeted if row["table"] in ("AccountsTrie", "StoragesTrie")
    ]

    def phase(name: str) -> float:
        return delta(before, after, name + "_sum")

    rw_commit = {"mode": "read-write", "outcome": "commit"}
    page_size = metric_value(after, "reth_db_page_size") or 4096.0
    dirty_bytes = delta(
        before, after, "reth_database_transaction_dirty_bytes_sum", rw_commit
    )
    page_ops = {}
    for operation in (
        "newly",
        "cow",
        "clone",
        "split",
        "merge",
        "spill",
        "unspill",
        "wops",
        "msync",
        "fsync",
        "prefault",
        "mincore",
    ):
        page_ops[operation] = int(
            round(
                delta(
                    before,
                    after,
                    f"reth_database_transaction_page_{operation}_total",
                    rw_commit,
                )
            )
        )

    memmove_cpu = [
        row for row in samply["top_cpu_leaves"] if "memmove" in row["key"].lower()
    ]
    output = {
        "schema_version": "reth-fpga-detail-analysis/v1",
        "run_id": manifest["run_id"],
        "wall_clock": manifest["wall_clock"],
        "cpu": manifest["cpu"],
        "trie_phases": {
            "merge_batch_seconds": phase(
                "reth_storage_providers_database_save_blocks_merge_trie_updates"
            ),
            "account_trie_write_seconds": phase(
                "reth_storage_providers_database_save_blocks_write_account_trie"
            ),
            "storage_trie_write_seconds": phase(
                "reth_storage_providers_database_save_blocks_write_storage_trie"
            ),
            "combined_trie_merge_and_write_seconds": phase(
                "reth_storage_providers_database_save_blocks_write_trie_updates"
            ),
        },
        "transaction_space": {
            "page_size_bytes": int(page_size),
            "dirty_bytes_sum_at_commit": int(round(dirty_bytes)),
            "dirty_page_equivalents_sum": dirty_bytes / page_size,
            "retired_bytes_sum_at_commit": int(
                round(
                    delta(
                        before,
                        after,
                        "reth_database_transaction_retired_bytes_sum",
                        rw_commit,
                    )
                )
            ),
            "rw_commit_count": int(
                round(
                    delta(
                        before,
                        after,
                        "reth_database_transaction_dirty_bytes_count",
                        rw_commit,
                    )
                )
            ),
        },
        "mdbx_page_ops": page_ops,
        "pwrite_evidence": {
            "mdbx_prefault_write_operations": page_ops["prefault"],
            "mdbx_steady_write_operations": page_ops["wops"],
            "minimum_prefault_write_bytes": page_ops["prefault"] * int(page_size),
            "exact_pwrite_bytes_available": False,
            "reason": "libMDBX's prefault counter counts pwrite/pwritev operations but does not expose each vector length. One database page per operation is a strict byte lower bound; device iostat bytes are not process-specific.",
            "device_iostat_estimated_total_bytes": manifest["disk"]["estimated_total_bytes"],
        },
        "targeted_operation_rows": targeted,
        "trie_operation_rows": trie_operations,
        "all_nonzero_operation_rows": operations,
        "memmove_cpu_leaves": memmove_cpu,
        "samply_window": samply["window"],
        "samply_top_cpu_leaves": samply["top_cpu_leaves"],
        "samply_top_offcpu_leaves": samply["top_offcpu_leaves"],
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"run_id": manifest["run_id"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
