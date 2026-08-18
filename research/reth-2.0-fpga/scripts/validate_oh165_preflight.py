#!/usr/bin/env python3
"""Fail-closed OH-165 frozen-input and macOS profiler preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import subprocess


EXPECTED = {
    "goal_prompt": "bff1c9c5103ed14e7c6ba7984f92edd520d67f1d91475716e2cc845f04a59a1f",
    "source_commit": "eb4c15e5e36d8776d46629beae4c0a69af7ab04f",
    "reth": "a990bb60d590fc56a895543bbab2f9465f1c26198ddb13579b32c67c9c6fe731",
    "reth_bench": "43fbab6fc50e1b04dca83ddaa5723569788b6822fff3c551a9c6f3b4c5f90768",
    "corpus": "5dc0e9cbc1f215b30866d2d11b320d33dfe4b3cc9c5db848b826366f83aeed5f",
    "ordered_block_hashes": "ae7026bc1c547b79b6b173cf148b73b1ed358f5d1a18d58d4eae7a9c1cbad0f5",
    "accepted_snapshot_plan": "9f640f0917305228672632fceb9b5f171d889fd49e4083fb0d11637f17e59e35",
    "accepted_snapshot_inventory": "72629e5d585fc8f2b4ac43fcac909728c8d2379c4b6ed220951851319884a21c",
    "snapshot_stage_checkpoints": "5a925899ca159042142bf7c409ef6d169762c0a53ad1a180a4f06441f6fe7713",
}

def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(argv: list[str], env: dict[str, str] | None = None) -> dict[str, object]:
    try:
        completed = subprocess.run(argv, text=True, capture_output=True, env=env, check=False)
    except OSError as error:
        return {"argv": argv, "exit_code": 127, "stdout": "", "stderr": str(error)}
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def range_checksums(corpus: pathlib.Path, first: int, last: int) -> dict[str, object]:
    records = hashlib.sha256()
    ordered_hashes = hashlib.sha256()
    count = 0
    with corpus.open("rb") as stream:
        for line in stream:
            block = json.loads(line)
            number = int(block["number"], 16)
            if first <= number <= last:
                records.update(line)
                ordered_hashes.update((block["hash"] + "\n").encode())
                count += 1
    return {
        "blocks": count,
        "from": first,
        "to": last,
        "record_bytes_sha256": records.hexdigest(),
        "ordered_hashes_newline_sha256": ordered_hashes.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the exact frozen Reth 2.0 profiling inputs before a trial."
    )
    parser.add_argument(
        "--base",
        required=True,
        type=pathlib.Path,
        help="Root containing source/reth-fixed, build/fixed, corpus, compatibility-data, and cpu-baseline.",
    )
    parser.add_argument("--goal-prompt", required=True, type=pathlib.Path)
    parser.add_argument("--samply", type=pathlib.Path, help="Optional explicit samply binary path.")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    base = args.base.resolve()
    paths = {
        "goal_prompt": args.goal_prompt,
        "source": base / "source/reth-fixed",
        "reth": base / "build/fixed/maxperf/reth",
        "reth_bench": base / "build/fixed/maxperf/reth-bench",
        "corpus": base / "corpus/corpus.jsonl",
        "corpus_lock": base / "corpus/corpus-lock.json",
        "snapshot": base / "compatibility-data",
        "baseline_summary": base / "cpu-baseline/summary.json",
        "snapshot_acceptance": base / "logs/grader-snapshot-canonical.log",
    }
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)

    corpus_lock = json.loads(paths["corpus_lock"].read_text())
    checks = {
        "goal_prompt_sha256": sha256(paths["goal_prompt"]),
        "source_commit": command(["git", "-C", str(paths["source"]), "rev-parse", "HEAD"])["stdout"],
        "reth_sha256": sha256(paths["reth"]),
        "reth_bench_sha256": sha256(paths["reth_bench"]),
        "corpus_sha256": sha256(paths["corpus"]),
        "ordered_block_hashes_sha256": corpus_lock["ordered_block_hashes_sha256"],
        "snapshot_directory_present": paths["snapshot"].is_dir(),
        "snapshot_database_present": (paths["snapshot"] / "db/mdbx.dat").is_file(),
        "snapshot_stage_checkpoints_sha256": sha256(base / "evidence/preflight/stage-checkpoints.txt"),
        "snapshot_acceptance": paths["snapshot_acceptance"].read_text().strip(),
        "baseline_summary_present": paths["baseline_summary"].is_file(),
        "baseline_trial_manifests_present": all(
            (base / f"cpu-baseline/trial-{trial}/manifest.json").is_file()
            for trial in (1, 2, 3)
        ),
        "same_filesystem_device": os.stat(output.parent).st_dev == os.stat(paths["snapshot"]).st_dev,
    }
    frozen_pass = all(
        [
            checks["goal_prompt_sha256"] == EXPECTED["goal_prompt"],
            checks["source_commit"] == EXPECTED["source_commit"],
            checks["reth_sha256"] == EXPECTED["reth"],
            checks["reth_bench_sha256"] == EXPECTED["reth_bench"],
            checks["corpus_sha256"] == EXPECTED["corpus"],
            checks["ordered_block_hashes_sha256"] == EXPECTED["ordered_block_hashes"],
            checks["snapshot_directory_present"],
            checks["snapshot_database_present"],
            checks["snapshot_stage_checkpoints_sha256"] == EXPECTED["snapshot_stage_checkpoints"],
            EXPECTED["accepted_snapshot_inventory"] in checks["snapshot_acceptance"],
            checks["baseline_summary_present"],
            checks["baseline_trial_manifests_present"],
            checks["same_filesystem_device"],
        ]
    )

    xcode_select = command(["xcode-select", "-p"])
    xctrace = command(["/usr/bin/xctrace", "list", "templates"])
    samply = (
        command([str(args.samply), "--version"])
        if args.samply is not None
        else command(["/usr/bin/which", "samply"])
    )
    # Samply is the required stack profiler for this outcome.  Instruments is supplementary and
    # its absence under CommandLineTools-only macOS must not block the run.
    profiler_pass = samply["exit_code"] == 0

    report = {
        "schema_version": "reth-fpga-oh165-preflight/v1",
        "contract": {"issue": "OH-165", "outcome": "SC-01-O1", "trial_budget_used": 0, "trial_budget": 4},
        "host": {
            "system": platform.system(),
            "release": platform.mac_ver()[0],
            "machine": platform.machine(),
            "xcode_select": xcode_select,
        },
        "frozen_input_guardrail": {"pass": frozen_pass, "expected": EXPECTED, "observed": checks},
        "discovery_selection": {
            "rule": "first contiguous 100 blocks of the accepted 500 measured-block range",
            "checksum_method": "SHA-256 over exact source JSONL record bytes; ordered hash checksum is SHA-256 over lowercase 0x block hashes followed by newline",
            "warmup": range_checksums(paths["corpus"], 25661164, 25661183),
            "measured": range_checksums(paths["corpus"], 25661184, 25661283),
            "warmup_excluded_from_measurement": True,
            "frozen_before_first_profiler_run": True,
        },
        "profiler_guardrail": {
            "pass": profiler_pass,
            "samply_probe": samply,
            "instruments_probe": xctrace,
            "instruments_required": False,
        },
        "verdict": "PASS" if frozen_pass and profiler_pass else "CRITICAL_BLOCKER",
        "critical_blocker": None if profiler_pass else "MACOS_PROFILER_EVIDENCE_UNAVAILABLE",
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"verdict": report["verdict"], "critical_blocker": report["critical_blocker"], "output": str(output)}))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
