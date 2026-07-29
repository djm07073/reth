#!/usr/bin/env python3
"""Shared fail-closed validation for the SC-01 CPU baseline."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_ROOT = Path(os.environ.get("RETH_FPGA_ARTIFACT_ROOT", ROOT / "artifacts/oh-96"))
INPUT_PREFLIGHT = ARTIFACT_ROOT / "input-preflight.json"
BASELINE_ROOT = ARTIFACT_ROOT / "cpu-baseline"

RETH_COMMIT = "eb4c15e5e36d8776d46629beae4c0a69af7ab04f"
CORPUS_SHA256 = "d7e91e27d946659e678da4d970c9c059d75bae11ea9addec22cbd043b56a7c96"
CORPUS_LOCK_SHA256 = "d79717b912aeb0826c06448fad1811dae7a65f824349890279a6a22c9b8f9b68"
ORDERED_HASH_SHA256 = "0c67edbed2dd6c42a0f1cb684800fd1d6de213f8bf0011722f59758467989280"
SNAPSHOT_IDENTITY = "reth-v2.0.0-mainnet-through-20999999"
SNAPSHOT_HEAD = 20_999_999
WARMUP_FROM, WARMUP_TO = 21_000_000, 21_000_099
MEASURED_FROM, MEASURED_TO = 21_000_100, 21_000_599
BLOCKER_ID = "BENCHMARK_INPUT_UNAVAILABLE"


class ValidationError(ValueError):
    """A deterministic SC-01 evidence violation."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def evidence_digest(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("evidence_sha256", None)
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def validate_input_preflight(path: Path = INPUT_PREFLIGHT) -> dict[str, Any]:
    report = load_json(path)
    require(report.get("schema_version") == "reth-fpga-input-preflight/v1", "preflight schema")
    require(report.get("blocker_id") == BLOCKER_ID, "preflight blocker id")
    require(report.get("result") == "BLOCKED", "preflight result must be BLOCKED")
    require(report.get("evidence_sha256") == evidence_digest(report), "preflight evidence digest")

    expected = report.get("expected", {})
    require(expected.get("reth_commit") == RETH_COMMIT, "expected Reth commit")
    require(expected.get("corpus_sha256") == CORPUS_SHA256, "expected corpus sha256")
    require(expected.get("corpus_lock_sha256") == CORPUS_LOCK_SHA256, "expected lock sha256")
    require(expected.get("ordered_block_hashes_sha256") == ORDERED_HASH_SHA256, "expected ordered hash sha256")
    require(expected.get("snapshot_logical_identity") == SNAPSHOT_IDENTITY, "expected snapshot identity")
    require(expected.get("snapshot_head") == SNAPSHOT_HEAD, "expected snapshot head")

    observed = report.get("observed", {})
    source = observed.get("source", {})
    require(source.get("commit") == RETH_COMMIT, "pinned source was not checked")
    corpus = observed.get("corpus", {})
    if corpus.get("present"):
        require(corpus.get("sha256") == CORPUS_SHA256, "observed corpus sha256")
        require(corpus.get("lock_sha256") == CORPUS_LOCK_SHA256, "observed corpus lock sha256")
        require(corpus.get("blocks") == 664, "observed corpus block count")
        if not corpus.get("permanent_url"):
            require(
                "permanent_corpus_artifact" in report.get("missing_required_material", []),
                "missing permanent corpus artifact was not named",
            )

    official = observed.get("official_snapshot_source", {})
    require(official.get("url") == "https://snapshots.reth.rs/", "official snapshot source")
    require(official.get("http_status") == 200, "official snapshot source status")
    require(official.get("exact_snapshot_head_listed") is False, "exact snapshot listing check")
    require(SNAPSHOT_HEAD not in official.get("offered_blocks", []), "exact snapshot unexpectedly offered")

    snapshot = observed.get("snapshot", {})
    require(snapshot.get("present") is False, "declared snapshot must be absent for this blocker")
    require(
        "snapshot_archive_and_inventory" in report.get("missing_required_material", []),
        "missing snapshot material was not named",
    )
    attempts = report.get("attempted_authorized_local_sources", [])
    require(isinstance(attempts, list) and attempts, "authorized local attempts were not recorded")
    authority = report.get("authority", {})
    require(authority.get("aws_or_paid_resources_used") is False, "paid resource use is forbidden")
    require(authority.get("credentials_used") is False, "credential use is forbidden")
    require(
        report.get("execution_decision") == "do_not_launch_primary_trials",
        "blocked preflight must prevent primary trials",
    )
    return report


REQUIRED_TRIAL_FILES = (
    "metrics/application.jsonl",
    "metrics/mdbx.jsonl",
    "metrics/kernel.csv",
    "metrics/storage.jsonl",
    "metrics/trie.jsonl",
    "profiles/perf.data",
    "profiles/cpu.pprof",
    "profiles/on_cpu_flamegraph.svg",
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def validate_baseline(root: Path = BASELINE_ROOT) -> dict[str, Any]:
    trials_dir = root / "trials"
    manifests = sorted(trials_dir.glob("*/manifest.json"))
    require(len(manifests) == 6, "exactly 6 primary trial manifests are required")
    latencies: list[float] = []
    total_gas = 0
    for path in manifests:
        trial = load_json(path)
        require(trial.get("schema_version") == "reth-fpga-cpu-trial/v1", f"{path}: schema")
        require(trial.get("valid") is True, f"{path}: trial is not valid")
        require(trial.get("reth_commit") == RETH_COMMIT, f"{path}: Reth commit")
        require(trial.get("warmup") == {"from": WARMUP_FROM, "to": WARMUP_TO, "blocks": 100}, f"{path}: warm-up")
        require(
            trial.get("measurement") == {"from": MEASURED_FROM, "to": MEASURED_TO, "blocks": 500},
            f"{path}: measurement range",
        )
        completion = trial.get("completion", {})
        require(completion.get("persisted_block") >= MEASURED_TO, f"{path}: persistence")
        require(completion.get("async_drained") is True, f"{path}: async drain")
        require(completion.get("database_sync_completed") is True, f"{path}: database sync")
        start = trial.get("first_measured_payload_monotonic_seconds")
        end = completion.get("monotonic_seconds")
        require(isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start, f"{path}: monotonic timestamps")
        latencies.append(float(end - start))
        require(isinstance(trial.get("total_gas"), int) and trial["total_gas"] > 0, f"{path}: total gas")
        total_gas += trial["total_gas"]
        trial_dir = path.parent
        for relative in REQUIRED_TRIAL_FILES:
            require((trial_dir / relative).is_file(), f"{path}: missing {relative}")
        require(
            (trial_dir / "profiles/offcpu.pprof").is_file()
            or (trial_dir / "profiles/io_wait_stacks.txt").is_file(),
            f"{path}: missing off-CPU/I/O-wait profile",
        )

    overhead = load_json(root / "profiling_overhead.json")
    require(overhead.get("schema_version") == "reth-fpga-profiling-overhead/v1", "profiling overhead schema")
    require(overhead.get("primary_overhead_pct", 101) <= 3, "primary instrumentation overhead exceeds 3%")
    summary = {
        "valid_trials": 6,
        "median_wait_for_persistence_e2e_wall_clock_seconds": statistics.median(latencies),
        "p50_seconds": _percentile(latencies, 0.50),
        "p90_seconds": _percentile(latencies, 0.90),
        "p99_seconds": _percentile(latencies, 0.99),
        "run_variance_seconds_squared": statistics.variance(latencies),
        "total_gas": total_gas,
        "mgas_per_second": (total_gas / 1_000_000) / sum(latencies),
    }
    recorded = load_json(root / "summary.json")
    for key, value in summary.items():
        require(recorded.get(key) == value, f"summary mismatch: {key}")
    return summary
