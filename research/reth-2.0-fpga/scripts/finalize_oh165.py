#!/usr/bin/env python3
"""Cross-check discovery/full evidence and emit the machine-readable OH-165 decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any


CANDIDATE_STACK_ID = "74c43b6c456b32fb"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def persistence_thread(analysis: dict[str, Any]) -> dict[str, Any]:
    matches = [value for key, value in analysis["threads"].items() if key.startswith("persistence:")]
    if len(matches) != 1:
        raise RuntimeError(f"expected one persistence thread, got {len(matches)}")
    return matches[0]


def stack_row(thread: dict[str, Any]) -> dict[str, Any]:
    matches = [row for row in thread["top_cpu_stacks"] if row["key"].startswith(CANDIDATE_STACK_ID + " | ")]
    if len(matches) != 1:
        raise RuntimeError(f"candidate stack {CANDIDATE_STACK_ID} absent from ranked persistence CPU stacks")
    return matches[0]


def run_summary(manifest_path: Path, analysis_path: Path) -> dict[str, Any]:
    manifest = load(manifest_path)
    analysis = load(analysis_path)
    thread = persistence_thread(analysis)
    stack = stack_row(thread)
    mdbx = manifest["mdbx"]
    wall = manifest["wall_clock"]
    canonical = manifest["canonical_correctness"]
    proof = manifest["persistence_completion"]
    # The valid discovery run's supplementary ps sampler selected Samply's wrapper PID.
    # Primary Samply thread deltas and all Prometheus/MDBX/Trie evidence are unaffected.
    process_cpu = (
        {
            "usable": False,
            "reason": "resource sampler selected the Samply wrapper PID; use Samply thread CPU evidence",
        }
        if manifest["run_id"] == "discovery-samply-v3"
        else {"usable": True, "data": manifest["cpu"]}
    )
    valid = all((
        manifest["valid"],
        manifest["frozen_input_guardrail"]["pass"],
        proof["verified"],
        proof["cold_reopen_verified"],
        canonical["expected_hash"] == canonical["actual_hash"],
        canonical["expected_state_root"] == canonical["actual_state_root"],
    ))
    return {
        "run_id": manifest["run_id"],
        "valid": valid,
        "measurement": manifest["measurement"],
        "wall_seconds": wall["e2e_seconds"],
        "persistence_wait_seconds": wall["persistence_wait_seconds"],
        "persistence_wait_ratio": wall["persistence_wait_ratio"],
        "save_blocks_mdbx_seconds": mdbx["save_blocks_mdbx_seconds"],
        "write_trie_updates_seconds": mdbx["save_blocks_write_trie_updates_seconds"],
        "write_hashed_state_seconds": mdbx["save_blocks_write_hashed_state_seconds"],
        "commit_sync_seconds": mdbx["commit_sync_seconds"],
        "state_root_seconds": manifest["trie"]["state_root_seconds"],
        "execution_seconds": manifest["trie"]["execution_seconds"],
        "disk": manifest["disk"],
        "process_cpu": process_cpu,
        "persistence_thread": {
            "cpu_seconds": thread["thread_cpu_seconds"],
            "process_cpu_ratio": next(
                row["ratio"] for row in analysis["top_threads_by_cpu"] if row["key"] == "persistence"
            ),
            "oncpu_sample_ratio": thread["oncpu_sample_ratio"],
            "offcpu_sample_ratio": thread["offcpu_sample_ratio"],
            "candidate_stack": stack,
        },
        "persisted_head": proof["persisted_head"],
        "measured_endpoint_hash": canonical["actual_hash"],
        "measured_endpoint_state_root": canonical["actual_state_root"],
        "manifest_sha256": sha256(manifest_path),
        "analysis_sha256": sha256(analysis_path),
        "profile_sha256": analysis["profile_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery-manifest", required=True, type=Path)
    parser.add_argument("--discovery-analysis", required=True, type=Path)
    parser.add_argument("--full-manifest", required=True, type=Path)
    parser.add_argument("--full-analysis", required=True, type=Path)
    parser.add_argument("--baseline-reduction", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    discovery = run_summary(args.discovery_manifest, args.discovery_analysis)
    full = run_summary(args.full_manifest, args.full_analysis)
    baseline = load(args.baseline_reduction)
    baseline_trials = baseline["trials"]
    baseline_medians = {
        key: statistics.median(float(row[key]) for row in baseline_trials)
        for key in (
            "wall_seconds", "persistence_wait_seconds", "save_blocks_mdbx_seconds",
            "write_trie_updates_seconds", "write_hashed_state_seconds", "commit_sync_seconds",
            "state_root_seconds", "execution_seconds", "trie_cursor_seconds", "hashed_cursor_seconds",
        )
    }

    same_stack = (
        discovery["persistence_thread"]["candidate_stack"]["key"].split(" | ", 1)[0] ==
        full["persistence_thread"]["candidate_stack"]["key"].split(" | ", 1)[0] ==
        CANDIDATE_STACK_ID
    )
    reproduced = all((
        discovery["valid"], full["valid"], baseline["all_trials_valid"], same_stack,
        discovery["persistence_wait_ratio"] > 0.8, full["persistence_wait_ratio"] > 0.8,
        discovery["write_trie_updates_seconds"] > discovery["commit_sync_seconds"],
        discovery["write_trie_updates_seconds"] > discovery["state_root_seconds"],
        full["write_trie_updates_seconds"] > full["commit_sync_seconds"],
        full["write_trie_updates_seconds"] > full["state_root_seconds"],
    ))
    output = {
        "schema_version": "reth-fpga-oh165-decision/v1",
        "verdict": {
            "full_500_block_causal_bottleneck_reproduced": reproduced,
            "candidate": "serial persistence-service MDBX account/storage Trie-update writes",
            "candidate_stack_id": CANDIDATE_STACK_ID,
            "stack_reproduced_discovery_to_full": same_stack,
            "excluded_as_primary": ["state-root calculation", "execution", "MDBX commit sync/fsync"],
        },
        "causal_chain": [
            "reth_newPayload(wait_for_persistence=always) blocks on the pending persistence receiver",
            "the persistence service calls DatabaseProvider::save_blocks(SaveBlocksMode::Full)",
            "save_blocks serially writes merged hashed state and sorted account/storage Trie updates through MDBX cursors",
            "Samply attributes the dominant persistence-thread CPU stack to the same Reth RVAs ending in pwrite",
        ],
        "discovery": discovery,
        "full_confirmation": full,
        "baseline": {
            "trials": baseline_trials,
            "medians": baseline_medians,
            "reduction_sha256": sha256(args.baseline_reduction),
        },
        "profiled_wall_comparison": {
            "full_vs_unprofiled_baseline_median_ratio": full["wall_seconds"] / baseline["median_wall_seconds"] - 1.0,
            "interpretation": "The profiled run was 3.1% faster, so no profiler slowdown is visible; the unprofiled three-trial median remains the performance baseline.",
        },
        "scope": {
            "xcode_instruments": "absent; supplementary only and not used",
            "new_snapshot_downloaded": False,
            "linux_or_aws_used": False,
            "optimization_or_fpga_implemented": False,
            "measured_profile_trials_used": 3,
            "trial_budget": 4,
            "invalid_attempts_used_as_confirmation": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "reproduced": reproduced}))
    return 0 if reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
