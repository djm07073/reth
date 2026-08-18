#!/usr/bin/env python3
"""Persist a measured tail when the frozen corpus has no future trigger blocks.

This is deliberately a durability-only recovery step. The measured Prometheus
boundary remains the one captured by ``profile_oh165.py`` before this script is
run. The script replays only blocks that are already inside the frozen corpus,
uses a temporary threshold that exactly matches the pending tail, and proves the
measured endpoint with a stopped-node cold reopen.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import profile_oh165 as profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", required=True, type=Path)
    parser.add_argument("--run-output", required=True, type=Path)
    parser.add_argument("--reth", required=True, type=Path)
    parser.add_argument("--reth-bench", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--corpus-lock", required=True, type=Path)
    parser.add_argument("--expected-persisted-head", required=True, type=int)
    parser.add_argument("--measured-end", required=True, type=int)
    parser.add_argument("--original-persistence-threshold", required=True, type=int)
    args = parser.parse_args()

    recovery = args.run_output / "locked-corpus-tail-recovery"
    if recovery.exists():
        raise RuntimeError(f"recovery output already exists: {recovery}")
    if not (args.datadir / "db/mdbx.dat").is_file():
        raise RuntimeError("datadir does not contain an MDBX database")
    jwt = args.run_output / "jwt.hex"
    if not jwt.is_file():
        raise RuntimeError("run output does not contain jwt.hex")

    tail_blocks = args.measured_end - args.expected_persisted_head
    if tail_blocks <= 0:
        raise RuntimeError("measured endpoint must be above the persisted head")
    temporary_threshold = tail_blocks - 1
    temporary_backpressure = max(16, temporary_threshold * 2)
    recovery.mkdir()

    fixture_script = Path(profile.__file__).resolve().parents[1] / "cp1-corpus/fixture_oh155.py"
    fixture_log = (recovery / "fixture.log").open("wb")
    fixture = subprocess.Popen(
        [
            "/usr/bin/python3",
            str(fixture_script),
            "--corpus",
            str(args.corpus),
            "--lock",
            str(args.corpus_lock),
        ],
        stdout=fixture_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    node_log = (recovery / "node.log").open("wb")
    node = subprocess.Popen(
        profile.node_command(
            args.reth,
            args.datadir,
            jwt,
            temporary_threshold,
            temporary_backpressure,
        ),
        stdout=node_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        profile.wait_rpc("http://127.0.0.1:8547", profile.HEAD + 600)
        actual_start = profile.wait_rpc(
            "http://127.0.0.1:8545", args.expected_persisted_head
        )
        profile.initialize_forkchoice(
            jwt,
            args.corpus,
            recovery / "initial-forkchoice.json",
            actual_start,
        )
        profile.run_logged(
            profile.bench_command(
                args.reth_bench,
                jwt,
                actual_start,
                args.measured_end,
                recovery / "tail-replay",
            ),
            recovery / "tail-replay.log",
        )
        profile.run_logged(
            profile.bench_command(
                args.reth_bench,
                jwt,
                args.measured_end - 1,
                args.measured_end,
                recovery / "tail-drain",
            ),
            recovery / "tail-drain.log",
        )
        (recovery / "metrics-after-tail.prom").write_text(profile.scrape_metrics())
    finally:
        if node.poll() is None:
            profile.stop_process_group(node)
        node_log.close()
        if fixture.poll() is None:
            profile.stop_process_group(fixture, timeout=10)
        fixture_log.close()

    reopen_log = (recovery / "cold-reopen.log").open("wb")
    verifier = subprocess.Popen(
        profile.node_command(
            args.reth,
            args.datadir,
            jwt,
            args.original_persistence_threshold,
            max(16, args.original_persistence_threshold * 2),
            metrics=False,
        ),
        stdout=reopen_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        persisted_head = profile.wait_rpc(
            "http://127.0.0.1:8545", args.measured_end
        )
        persisted_block = profile.rpc(
            "http://127.0.0.1:8545",
            "eth_getBlockByNumber",
            [hex(args.measured_end), False],
        )
    finally:
        verifier_exit = profile.stop_process_group(verifier)
        reopen_log.close()

    expected = profile.expected_block(args.corpus, args.measured_end)
    correctness = {
        "block": args.measured_end,
        "expected_hash": expected["hash"].lower(),
        "actual_hash": persisted_block["hash"].lower(),
        "expected_state_root": expected["stateRoot"].lower(),
        "actual_state_root": persisted_block["stateRoot"].lower(),
    }
    valid = (
        verifier_exit == 0
        and persisted_head == args.measured_end
        and correctness["expected_hash"] == correctness["actual_hash"]
        and correctness["expected_state_root"] == correctness["actual_state_root"]
    )
    manifest = {
        "schema_version": "reth-fpga-locked-corpus-tail-recovery/v1",
        "valid": valid,
        "method": "replay only the pending measured tail with an exact temporary threshold, then cold reopen",
        "expected_persisted_head": args.expected_persisted_head,
        "measured_end": args.measured_end,
        "tail_blocks": tail_blocks,
        "temporary_persistence_threshold": temporary_threshold,
        "temporary_batch_blocks": tail_blocks,
        "original_persistence_threshold": args.original_persistence_threshold,
        "cold_reopen_persisted_head": persisted_head,
        "canonical_correctness": correctness,
    }
    (recovery / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(manifest, sort_keys=True))
    if not valid:
        raise RuntimeError("locked-corpus tail recovery failed correctness validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
