#!/usr/bin/env python3
"""Validate the frozen CP-0 experiment contract using only the standard library."""

from __future__ import annotations

import csv
import json
import re
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
METRICS = ROOT / "metrics.csv"
RETH_EVIDENCE = ROOT / "reth-evidence.csv"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def flag_value(command: str, flag: str) -> str:
    tokens = shlex.split(command)
    require(flag in tokens, f"{flag} missing from command")
    index = tokens.index(flag)
    require(index + 1 < len(tokens), f"{flag} value missing from command")
    return tokens[index + 1]


with MANIFEST.open(encoding="utf-8") as source:
    manifest = json.load(source)

require(manifest["schema_version"] == "reth-fpga-experiment-contract/v1", "schema_version")
require(manifest["goal_stage"] == "SC-01", "goal_stage")
require(manifest["success_criterion"] == "SC-01.1", "success_criterion")
require(manifest["paid_resource_policy"]["aws_f1_launch_allowed"] is False, "paid resource guard")
require(
    manifest["paid_resource_policy"]["other_paid_resource_launch_allowed"] is False,
    "other paid resource guard",
)

software = manifest["software"]
require(software["source_repository"] == "https://github.com/djm07073/reth",
        "research repository")
require(software["upstream_repository"] == "https://github.com/paradigmxyz/reth",
        "upstream repository")
require(software["upstream_release_ref"] == "refs/tags/v2.0.0", "release ref")
require(
    software["reth_commit"] == "eb4c15e5e36d8776d46629beae4c0a69af7ab04f",
    "release commit",
)
require(re.fullmatch(r"[0-9a-f]{40}", software["reth_commit"]) is not None, "reth commit")
require(software["reth_commit"] == software["reth_bench_commit"], "bench revision")
require(software["cargo_locked"] is True, "Cargo.lock policy")
require(
    software["cargo_lock_sha256"] ==
    "c43cfd1ec673a6b8a9ed92d70b3ccc767759ff92bc5819fbffe6e1f2d2d5210e",
    "Cargo.lock hash",
)
for package, binary in (("reth", "reth"), ("reth_bench", "reth-bench")):
    build_command = software["build_commands"][package]
    require("cargo +1.93.0 build --locked --profile maxperf" in build_command, f"{package} build")
    require(f"--package {binary} --bin {binary}" in build_command, f"{package} package")

workload = manifest["workload"]
warmup = workload["warmup"]
measurement = workload["measurement"]
require(warmup["to"] - warmup["from"] + 1 == warmup["blocks"], "warmup range")
require(measurement["to"] - measurement["from"] + 1 == measurement["blocks"], "measurement range")
require(warmup["to"] + 1 == measurement["from"], "contiguous ranges")
require(workload["snapshot_head"]["number"] + 1 == warmup["from"], "snapshot boundary")
require(
    workload["payload_source"]["from"] <= warmup["from"] - 64,
    "safe/finalized lookback coverage",
)
require(workload["payload_source"]["to"] == measurement["to"], "fixture upper bound")
mode_names = {mode["name"] for mode in workload["modes"]}
require({"live-sync", "wait-for-persistence"} <= mode_names, "workload modes")
require(workload["completion_condition"]["persisted_block_at_least"] == measurement["to"], "completion")
for artifact in ("payload_source", "snapshot"):
    require("artifact_sha256" in workload[artifact]["required_lock_fields"], f"{artifact} hash lock")

commands = workload["commands"]
expected_ranges = {
    "live_sync_warmup": (workload["snapshot_head"]["number"], warmup["to"]),
    "persistence_warmup": (workload["snapshot_head"]["number"], warmup["to"]),
    "live_sync_measurement": (warmup["to"], measurement["to"]),
    "persistence_measurement": (warmup["to"], measurement["to"]),
}
for name, (anchor, end) in expected_ranges.items():
    command = commands[name]
    require(flag_value(command, "--from") == str(anchor), f"{name} anchor")
    require(flag_value(command, "--to") == str(end), f"{name} end")
    require(int(flag_value(command, "--to")) - int(flag_value(command, "--from")) in
            {warmup["blocks"], measurement["blocks"]}, f"{name} count")
    require(flag_value(command, "--rpc-block-fetch-retries") == "0", f"{name} offline retry")
    require(flag_value(command, "--rpc-url").startswith("http://127.0.0.1:"), f"{name} fixture")
    require(flag_value(command, "--output").startswith("artifacts/<run_id>/"), f"{name} output")
    require(not flag_value(command, "--output").endswith(".csv"), f"{name} output directory")
for name in ("persistence_warmup", "persistence_measurement"):
    tokens = shlex.split(commands[name])
    require("--reth-new-payload" in tokens, f"{name} reth namespace")
    require(flag_value(commands[name], "--wait-for-persistence") == "always", f"{name} wait")
for name in ("live_sync_warmup", "live_sync_measurement"):
    require("--reth-new-payload" not in shlex.split(commands[name]), f"{name} engine namespace")

repetition = manifest["repetition"]
require(repetition["independent_pairs_per_cell"] >= 6, "repeated-run count")
require(
    len(repetition["pair_orders"]) == repetition["independent_pairs_per_cell"],
    "pair order count",
)
require(set(repetition["comparison_cells"]["modes"]) == mode_names, "repetition modes")
require(
    set(repetition["comparison_cells"]["cache_states"]) ==
    {state["name"] for state in manifest["environment"]["cache_states"]},
    "repetition cache states",
)

statistics = set(manifest["statistics"]["report"])
require({"median", "p95", "sample_variance", "95%_paired_bootstrap_confidence_interval"} <= statistics,
        "statistical report")
require(manifest["statistics"]["minimum_valid_pairs"] >= 6, "minimum valid pairs")
require(
    manifest["statistics"]["minimum_valid_pairs"] ==
    repetition["independent_pairs_per_cell"],
    "valid pairs per cell",
)
primary_cell = manifest["statistics"]["primary_success_cell"]
require(primary_cell["mode"] == "wait-for-persistence", "primary mode")
require(primary_cell["cache_state"] == "warm", "primary cache state")
require(primary_cell["metric"] == "e2e_wall_clock", "primary endpoint")
require(
    manifest["statistics"]["success_threshold"]["minimum_median_improvement_percent"] == 25,
    "success threshold",
)
require(
    manifest["statistics"]["performance_no_regression"]
    ["maximum_median_e2e_degradation_percent"] <= 3,
    "no-regression guardrail",
)

comparison = manifest["comparison"]
for key in (
    "same_reth_bench_binary_sha256",
    "same_payload_corpus_sha256",
    "same_snapshot_sha256",
    "same_cache_state",
):
    require(comparison[key] is True, key)
required_boundary = {
    "FPGA runtime",
    "PCIe/DMA",
    "serialization",
    "queueing",
    "synchronization",
    "CPU orchestration",
    "timeout",
    "CPU fallback",
}
require(required_boundary <= set(comparison["candidate_boundary_includes"]), "FPGA boundary")

correctness = manifest["correctness"]
require(correctness["per_payload_status"] == "VALID", "payload correctness")
require({"execution result", "state root"} <= set(correctness["compare_fields"]), "oracle fields")
require(correctness["fallback"]["required"] is True, "fallback")
crash_safety = correctness["fallback"]["crash_safety"].lower()
require({"restart", "committed"} <= set(crash_safety.replace(";", " ").split()), "crash safety")

environment = manifest["environment"]
host = environment["comparison_host"]
require(host["instance_type"] == "f1.2xlarge", "host type")
require(host["region"] == "us-east-1", "host region")
require(host["vcpu_count"] == 8 and host["memory_gib"] == 122, "host shape")
require(environment["storage"]["nominal_capacity_gb"] == 470, "storage capacity")
require("fit_gate" in environment["storage"], "snapshot fit gate")

with METRICS.open(newline="", encoding="utf-8") as source:
    rows = list(csv.DictReader(source))

required_columns = {
    "metric_id",
    "layer",
    "source",
    "unit",
    "sampling",
    "expected_overhead",
    "artifact_path",
    "run_class",
}
require(rows and set(rows[0]) == required_columns, "metric columns")
require(all(all(row[column].strip() for column in required_columns) for row in rows), "empty metric cell")
require(len({row["metric_id"] for row in rows}) == len(rows), "duplicate metric id")
layers = {row["layer"] for row in rows}
require({"application", "kernel", "mdbx", "trie", "fpga"} <= layers, "metric layers")
metric_ids = {row["metric_id"] for row in rows}
require(
    {
        "e2e_wall_clock",
        "persistence_backpressure",
        "worker_wait",
        "mdbx_txn_commit",
        "mdbx_cursor_seek",
        "mdbx_scan_shape",
        "mdbx_dependency_chain",
        "mdbx_pages_per_op",
        "trie_traversal",
        "trie_hashing",
        "trie_dependency_chain",
        "cycles",
        "instructions_ipc",
        "memory_bandwidth",
        "page_faults",
        "futex_offcpu",
        "block_io",
        "fpga_kernel_latency",
        "pcie_dma",
        "fpga_fallback",
        "fpga_batchability_projection",
        "fpga_projected_transfer",
    } <= metric_ids,
    "required metric coverage",
)

with RETH_EVIDENCE.open(newline="", encoding="utf-8") as source:
    evidence_rows = list(csv.DictReader(source))

evidence_columns = {
    "pr",
    "url",
    "status",
    "merged_commit",
    "baseline_relation",
    "evidence_type",
    "reported_signal",
    "contract_interpretation",
    "required_metrics",
}
require(evidence_rows and set(evidence_rows[0]) == evidence_columns, "evidence columns")
require({row["pr"] for row in evidence_rows} == {"25595", "22623", "25854", "23045", "22077"},
        "evidence PR set")
expected_evidence = {
    "25595": ("merged", "after-v2.0.0", "counterfactual-paired-replay"),
    "22623": ("closed-unmerged", "not-in-v2.0.0", "microbenchmark"),
    "25854": ("closed-unmerged", "not-in-v2.0.0", "paired-replay-negative"),
    "23045": (
        "closed-unmerged",
        "not-in-v2.0.0",
        "hardware-counter-and-paired-replay",
    ),
    "22077": ("merged", "in-v2.0.0", "paired-replay"),
}
for row in evidence_rows:
    require(
        row["url"] == f"https://github.com/paradigmxyz/reth/pull/{row['pr']}",
        f"PR {row['pr']} URL",
    )
    require(
        (row["status"], row["baseline_relation"], row["evidence_type"]) ==
        expected_evidence[row["pr"]],
        f"PR {row['pr']} classification",
    )
    require(row["reported_signal"].strip(), f"PR {row['pr']} signal")
    require(row["contract_interpretation"].strip(), f"PR {row['pr']} interpretation")
    if row["status"] == "merged":
        require(re.fullmatch(r"[0-9a-f]{40}", row["merged_commit"]) is not None,
                f"PR {row['pr']} merge commit")
    else:
        require(not row["merged_commit"], f"PR {row['pr']} unmerged commit")
    referenced_metrics = set(row["required_metrics"].split(";"))
    require(referenced_metrics <= metric_ids, f"PR {row['pr']} metric mapping")

print(
    f"OK {manifest['contract_id']}: "
    f"{len(rows)} metrics, {len(evidence_rows)} PRs, "
    f"{repetition['independent_pairs_per_cell']} pairs/cell, "
    f"{warmup['blocks']} warmup + {measurement['blocks']} measured blocks"
)
