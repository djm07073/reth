#!/usr/bin/env python3
"""Run one frozen OH-165 discovery or full-workload profiling trial."""

from __future__ import annotations

import argparse
import base64
import collections
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import signal
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


HEAD = 25_661_083
HEAD_HASH = "0x5986bec7d5900b9ae62ca86d5631a042c5b50be407b4beb09d9dc1a61f776f6f"
SETUP_END = HEAD + 80
MEASURED_START = HEAD + 100
SOURCE_COMMIT = "eb4c15e5e36d8776d46629beae4c0a69af7ab04f"
EXPECTED_RETH = "a990bb60d590fc56a895543bbab2f9465f1c26198ddb13579b32c67c9c6fe731"
EXPECTED_BENCH = "43fbab6fc50e1b04dca83ddaa5723569788b6822fff3c551a9c6f3b4c5f90768"
EXPECTED_CORPUS = "5dc0e9cbc1f215b30866d2d11b320d33dfe4b3cc9c5db848b826366f83aeed5f"
EXPECTED_ORDERED_HASHES = "ae7026bc1c547b79b6b173cf148b73b1ed358f5d1a18d58d4eae7a9c1cbad0f5"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rpc(url: str, method: str, params: list[Any], authorization: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if "error" in payload:
        raise RuntimeError(f"RPC {method} failed: {payload['error']}")
    return payload["result"]


def jwt_token(jwt: Path) -> str:
    def encode(value: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")

    header = encode({"alg": "HS256", "typ": "JWT"})
    claims = encode({"iat": int(time.time())})
    signing_input = header + b"." + claims
    signature = base64.urlsafe_b64encode(
        hmac.new(bytes.fromhex(jwt.read_text().strip()), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return "Bearer " + (signing_input + b"." + signature).decode()


def wait_rpc(url: str, expected: int | None = None, timeout: int = 600) -> int:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            block = int(rpc(url, "eth_blockNumber", []), 16)
            if expected is None or block == expected:
                return block
            last_error = RuntimeError(f"head {block}, expected {expected}")
        except Exception as error:
            last_error = error
        time.sleep(2)
    raise RuntimeError(f"RPC did not become ready: {last_error}")


def initialize_forkchoice(
    jwt: Path,
    corpus: Path,
    output: Path,
    head_number: int = HEAD,
) -> dict[str, Any]:
    wanted = {head_number, head_number - 32, head_number - 63}
    hashes: dict[int, str] = {}
    with corpus.open() as stream:
        for line in stream:
            row = json.loads(line)
            number = int(row["number"], 16)
            if number in wanted:
                hashes[number] = row["hash"].lower()
    state = {
        "headBlockHash": hashes[head_number],
        "safeBlockHash": hashes[head_number - 32],
        "finalizedBlockHash": hashes[head_number - 63],
    }
    attempts = []
    deadline = time.monotonic() + 600
    while True:
        response = rpc(
            "http://127.0.0.1:8551",
            "engine_forkchoiceUpdatedV3",
            [state, None],
            jwt_token(jwt),
        )
        status = response.get("payloadStatus", {})
        attempts.append({"monotonic_ns": time.monotonic_ns(), "status": status.get("status")})
        if status.get("status") == "VALID":
            if status.get("latestValidHash", "").lower() != hashes[head_number]:
                raise RuntimeError(f"initial forkchoice mismatch: {response}")
            break
        if status.get("status") != "SYNCING" or time.monotonic() >= deadline:
            raise RuntimeError(f"initial forkchoice did not become VALID: {response}")
        time.sleep(2)
    evidence = {"method": "engine_forkchoiceUpdatedV3", "state": state, "attempts": attempts, "response": response}
    output.write_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
    return evidence


def node_command(
    reth: Path,
    datadir: Path,
    jwt: Path,
    persistence_threshold: int,
    persistence_backpressure_threshold: int,
    metrics: bool = True,
) -> list[str]:
    command = [
        str(reth), "node", "--datadir", str(datadir), "--chain", "mainnet", "--debug.tip", HEAD_HASH,
        "--disable-discovery", "--max-outbound-peers", "0", "--max-inbound-peers", "0",
        "--http", "--http.addr", "127.0.0.1", "--http.port", "8545", "--http.api", "eth,reth,debug",
        "--ws", "--ws.addr", "127.0.0.1", "--ws.port", "8546", "--ws.api", "eth,reth",
        "--authrpc.addr", "127.0.0.1", "--authrpc.port", "8551", "--authrpc.jwtsecret", str(jwt),
        "--engine.accept-execution-requests-hash",
        "--db.sync-mode", "durable",
        "--engine.persistence-threshold", str(persistence_threshold),
        "--engine.persistence-backpressure-threshold", str(persistence_backpressure_threshold),
        "--engine.memory-block-buffer-target", "0",
    ]
    if metrics:
        command += ["--metrics", "127.0.0.1:9001"]
    return command


def bench_command(
    bench: Path,
    jwt: Path,
    start_parent: int,
    end: int,
    output: Path,
    *,
    request_mode: str = "forced-persistence",
    block_interval_ms: int = 0,
) -> list[str]:
    command = [
        str(bench), "new-payload-fcu", "--rpc-url", "http://127.0.0.1:8547",
        "--from", str(start_parent), "--to", str(end), "--jwt-secret", str(jwt),
        "--engine-rpc-url", "http://127.0.0.1:8551",
        "--metrics-url", "http://127.0.0.1:9001/metrics",
        "--rpc-block-fetch-retries", "0", "--rpc-block-buffer-size", "1", "--output", str(output),
    ]
    if request_mode == "forced-persistence":
        command += ["--reth-new-payload", "--wait-for-persistence", "always"]
    elif request_mode == "production-standard":
        if block_interval_ms > 0:
            command += ["--wait-time", f"{block_interval_ms}ms"]
    else:
        raise ValueError(f"unsupported request mode: {request_mode}")
    return command


def run_logged(command: list[str], log_path: Path, timeout: int = 10_800) -> None:
    with log_path.open("wb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}; see {log_path}")


def stop_process_group(process: subprocess.Popen[Any], timeout: int = 300) -> int:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        return process.wait(timeout=30)


def find_node_pid(datadir: Path, timeout: int = 60) -> int:
    deadline = time.monotonic() + timeout
    needle = f"--datadir {datadir}"
    while time.monotonic() < deadline:
        output = subprocess.check_output(["/bin/ps", "-axo", "pid=,command="], text=True)
        for line in output.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2:
                continue
            command = fields[1]
            # Samply's wrapper command embeds the entire child command line, including
            # `/reth node` and the datadir. Select the actual executable at argv[0].
            if needle in command and re.match(r"^\S*/reth node(?:\s|$)", command):
                return int(fields[0])
        time.sleep(0.5)
    raise RuntimeError("could not resolve profiled reth pid")


def process_cpu_seconds(pid: int) -> float:
    value = subprocess.check_output(["/bin/ps", "-o", "time=", "-p", str(pid)], text=True).strip()
    days = 0
    if "-" in value:
        day, value = value.split("-", 1)
        days = int(day)
    fields = [float(item) for item in value.split(":")]
    seconds = 0.0
    for item in fields:
        seconds = seconds * 60 + item
    return days * 86_400 + seconds


class ResourceSampler:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.rows: list[dict[str, float | int | str]] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                output = subprocess.check_output(
                    ["/bin/ps", "-o", "%cpu=,rss=,time=,state=,wchan=", "-p", str(self.pid)], text=True
                ).strip()
                fields = output.split()
                if len(fields) >= 4:
                    self.rows.append({
                        "monotonic_ns": time.monotonic_ns(),
                        "cpu_pct": float(fields[0]),
                        "rss_bytes": int(fields[1]) * 1024,
                        "state": fields[3],
                        "wait_channel": " ".join(fields[4:]) if len(fields) > 4 else "",
                    })
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass
            self.stop_event.wait(0.5)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


PRODUCTION_METRICS = (
    "reth_blockchain_tree_canonical_chain_height",
    "reth_blockchain_tree_in_mem_state_num_blocks",
    "reth_blockchain_tree_in_mem_state_earliest_block",
    "reth_blockchain_tree_in_mem_state_latest_block",
    "reth_consensus_engine_beacon_backpressure_active",
    "reth_consensus_engine_beacon_backpressure_stall_duration_sum",
    "reth_consensus_engine_beacon_backpressure_stall_duration_count",
    "reth_consensus_engine_persistence_save_blocks_duration_seconds_sum",
    "reth_consensus_engine_persistence_save_blocks_duration_seconds_count",
    "reth_consensus_engine_persistence_save_blocks_batch_size_sum",
    "reth_consensus_engine_persistence_save_blocks_batch_size_count",
)


class PrometheusSampler:
    """Low-rate sampler for production-path persistence lag and backpressure."""

    def __init__(self) -> None:
        self.rows: list[dict[str, float | int | str]] = []
        self.phase = "measurement"
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                samples = parse_prometheus(scrape_metrics())
                row: dict[str, float | int | str] = {
                    "monotonic_ns": time.monotonic_ns(),
                    "phase": self.phase,
                }
                for name in PRODUCTION_METRICS:
                    row[name] = samples.get(name, 0.0)
                self.rows.append(row)
            except (OSError, ValueError):
                pass
            self.stop_event.wait(1.0)

    def start(self) -> None:
        self.thread.start()

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


def scrape_metrics() -> str:
    with urllib.request.urlopen("http://127.0.0.1:9001/metrics", timeout=30) as response:
        return response.read().decode()


def parse_prometheus(text: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                samples[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return samples


def metric_delta(before: dict[str, float], after: dict[str, float], needles: tuple[str, ...]) -> float:
    return max(0.0, sum(
        value - before.get(name, 0.0)
        for name, value in after.items()
        if name.split("{", 1)[0].endswith("_sum") and all(n in name.split("{", 1)[0] for n in needles)
    ))


def metric_count_delta(before: dict[str, float], after: dict[str, float], needles: tuple[str, ...]) -> int:
    return max(0, round(sum(
        value - before.get(name, 0.0)
        for name, value in after.items()
        if name.split("{", 1)[0].endswith("_count") and all(n in name.split("{", 1)[0] for n in needles)
    )))


def metric_base_delta(before: dict[str, float], after: dict[str, float], base: str) -> float:
    """Sum a Prometheus summary/histogram delta across labels for one exact metric base."""
    wanted = base + "_sum"
    return max(0.0, sum(
        value - before.get(name, 0.0)
        for name, value in after.items()
        if name.split("{", 1)[0] == wanted
    ))


def metric_base_count_delta(before: dict[str, float], after: dict[str, float], base: str) -> int:
    wanted = base + "_count"
    return max(0, round(sum(
        value - before.get(name, 0.0)
        for name, value in after.items()
        if name.split("{", 1)[0] == wanted
    )))


def parse_iostat(path: Path, elapsed: float) -> dict[str, Any]:
    rows: list[tuple[float, float, float]] = []
    for line in path.read_text(errors="replace").splitlines():
        fields = line.split()
        if len(fields) == 3:
            try:
                rows.append(tuple(float(field) for field in fields))
            except ValueError:
                pass
    intervals = rows[1:] if len(rows) > 1 else rows
    avg_iops = statistics.fmean(row[1] for row in intervals) if intervals else 0.0
    avg_mib_s = statistics.fmean(row[2] for row in intervals) if intervals else 0.0
    return {
        "tool": "/usr/sbin/iostat -d -w 1 disk0",
        "samples": len(intervals),
        "average_iops": avg_iops,
        "average_mib_per_second": avg_mib_s,
        "estimated_total_bytes": round(avg_mib_s * elapsed * 1024 * 1024),
    }


def expected_block(corpus: Path, number: int) -> dict[str, Any]:
    with corpus.open() as stream:
        for line in stream:
            block = json.loads(line)
            if int(block["number"], 16) == number:
                return block
    raise RuntimeError(f"block {number} missing from corpus")


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    lock = json.loads(args.corpus_lock.read_text())
    source_diff = subprocess.check_output(
        ["git", "-C", str(args.source), "diff", "--binary", "HEAD"],
    )
    observed = {
        "source_commit": subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip(),
        "reth_sha256": sha256(args.reth),
        "reth_bench_sha256": sha256(args.reth_bench),
        "corpus_sha256": sha256(args.corpus),
        "ordered_block_hashes_sha256": lock["ordered_block_hashes_sha256"],
        "snapshot_database_present": (args.snapshot_datadir / "db/mdbx.dat").is_file(),
        "same_filesystem_device": os.stat(args.output.parent).st_dev == os.stat(args.snapshot_datadir).st_dev,
    }
    expected = {
        "source_commit": args.expected_source_commit,
        "reth_sha256": args.expected_reth_sha,
        "reth_bench_sha256": EXPECTED_BENCH,
        "corpus_sha256": EXPECTED_CORPUS,
        "ordered_block_hashes_sha256": EXPECTED_ORDERED_HASHES,
        "snapshot_database_present": True,
        "same_filesystem_device": True,
    }
    if observed != expected:
        raise RuntimeError(f"frozen input mismatch: observed={observed}")
    return {
        "pass": True,
        "expected": expected,
        "observed": observed,
        "source_dirty": bool(source_diff),
        "source_diff_sha256": hashlib.sha256(source_diff).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--measured-blocks", required=True, type=int, choices=(100, 500))
    parser.add_argument("--persistence-threshold", type=int, default=2)
    parser.add_argument(
        "--measurement-request-mode",
        choices=("forced-persistence", "production-standard"),
        default="forced-persistence",
        help="Engine API path used only for the measured block range.",
    )
    parser.add_argument(
        "--block-interval-ms",
        type=int,
        default=0,
        help="Minimum newPayload-to-next-block interval in production-standard mode.",
    )
    parser.add_argument("--profiler", choices=("none", "samply"), required=True)
    parser.add_argument("--snapshot-datadir", required=True, type=Path)
    parser.add_argument(
        "--prepared-datadir",
        type=Path,
        help="Stopped derived datadir already at the deterministic measurement boundary.",
    )
    parser.add_argument("--datadir", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--reth", required=True, type=Path)
    parser.add_argument("--expected-reth-sha", default=EXPECTED_RETH)
    parser.add_argument("--reth-bench", required=True, type=Path)
    parser.add_argument("--samply", type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--corpus-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.persistence_threshold < 0:
        raise RuntimeError("--persistence-threshold must be non-negative")
    if args.block_interval_ms < 0:
        raise RuntimeError("--block-interval-ms must be non-negative")
    if args.measurement_request_mode == "forced-persistence" and args.block_interval_ms:
        raise RuntimeError("--block-interval-ms is only valid with production-standard mode")
    if args.prepared_datadir is not None and not (args.prepared_datadir / "db/mdbx.dat").is_file():
        raise RuntimeError("--prepared-datadir does not contain an MDBX database")
    persistence_backpressure_threshold = max(16, args.persistence_threshold * 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.datadir.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() or args.datadir.exists():
        raise RuntimeError("output or trial datadir already exists")
    args.output.mkdir()
    frozen_guard = preflight(args)
    measured_end = MEASURED_START + args.measured_blocks

    clone_started = time.monotonic()
    clone_source = args.prepared_datadir or args.snapshot_datadir
    subprocess.run(["/bin/cp", "-cR", str(clone_source), str(args.datadir)], check=True)
    clone_seconds = time.monotonic() - clone_started
    jwt = args.output / "jwt.hex"
    jwt.write_text(secrets.token_hex(32))
    jwt.chmod(0o600)

    fixture_log = (args.output / "fixture.log").open("wb")
    fixture_script = Path(__file__).resolve().parents[1] / "cp1-corpus/fixture_oh155.py"
    fixture = subprocess.Popen(
        ["/usr/bin/python3", str(fixture_script), "--corpus", str(args.corpus), "--lock", str(args.corpus_lock)],
        stdout=fixture_log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    node_log = (args.output / "node.log").open("wb")
    command = node_command(
        args.reth,
        args.datadir,
        jwt,
        args.persistence_threshold,
        persistence_backpressure_threshold,
    )
    profile_path = args.output / "samply-profile.json.gz"
    if args.profiler == "samply":
        if args.samply is None:
            raise RuntimeError("--samply is required")
        command = [
            str(args.samply), "record", "--save-only", "--unstable-presymbolicate",
            "--cswitch-markers", "--reuse-threads", "--fold-recursive-prefix", "--rate", "99",
            "--output", str(profile_path), "--", *command,
        ]
    node_wrapper = subprocess.Popen(command, stdout=node_log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_rpc("http://127.0.0.1:8547", HEAD + 600)
        expected_start_head = None if args.prepared_datadir is not None else HEAD
        start_head = wait_rpc("http://127.0.0.1:8545", expected_start_head)
        node_pid = find_node_pid(args.datadir)
        if args.prepared_datadir is not None:
            if not MEASURED_START - args.persistence_threshold <= start_head <= MEASURED_START:
                raise RuntimeError(
                    f"prepared datadir head {start_head} is outside the recoverable measurement boundary"
                )
            initial_forkchoice = initialize_forkchoice(
                jwt,
                args.corpus,
                args.output / "initial-forkchoice.json",
                start_head,
            )
            if start_head < MEASURED_START:
                run_logged(
                    bench_command(
                        args.reth_bench,
                        jwt,
                        start_head,
                        MEASURED_START,
                        args.output / "prepared-boundary-repair",
                    ),
                    args.output / "prepared-boundary-repair.log",
                )
        else:
            initial_forkchoice = initialize_forkchoice(
                jwt,
                args.corpus,
                args.output / "initial-forkchoice.json",
            )
            run_logged(bench_command(args.reth_bench, jwt, HEAD, SETUP_END, args.output / "setup-80"), args.output / "setup-80.log")
            run_logged(bench_command(args.reth_bench, jwt, SETUP_END, MEASURED_START, args.output / "warmup-20"), args.output / "warmup-20.log")
        if int(rpc("http://127.0.0.1:8545", "eth_blockNumber", []), 16) != MEASURED_START:
            raise RuntimeError("warm-up did not reach deterministic discovery boundary")

        before_text = scrape_metrics()
        (args.output / "metrics-before.prom").write_text(before_text)
        before = parse_prometheus(before_text)
        iostat_path = args.output / "iostat.txt"
        iostat_log = iostat_path.open("wb")
        iostat = subprocess.Popen(
            ["/usr/sbin/iostat", "-d", "-w", "1", "disk0"],
            stdout=iostat_log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        sampler = ResourceSampler(node_pid)
        sampler.start()
        persistence_sampler = PrometheusSampler()
        persistence_sampler.start()
        cpu_before = process_cpu_seconds(node_pid)
        start_wall = utc_now()
        start_epoch_ms = time.time_ns() / 1e6
        start_ns = time.monotonic_ns()

        run_logged(
            bench_command(
                args.reth_bench,
                jwt,
                MEASURED_START,
                measured_end,
                args.output / "measured",
                request_mode=args.measurement_request_mode,
                block_interval_ms=args.block_interval_ms,
            ),
            args.output / "measured.log",
        )
        injection_ns = time.monotonic_ns()
        injection_epoch_ms = time.time_ns() / 1e6
        injection_wall = utc_now()
        cpu_injection = process_cpu_seconds(node_pid)
        injection_text = scrape_metrics()
        (args.output / "metrics-injection-end.prom").write_text(injection_text)
        injection_metrics = parse_prometheus(injection_text)
        persistence_sampler.set_phase("drain")
        # Persistence triggers when the canonical-minus-persisted gap exceeds the configured
        # threshold, giving a threshold+1 cadence when the memory block buffer target is zero.
        # Submit the minimum excluded future corpus blocks needed to trigger the final pending
        # batch, then duplicate the last block to wait for its in-flight save.  This preserves a
        # clean measured boundary while proving the measured endpoint is durable.
        persistence_batch_blocks = args.persistence_threshold + 1
        blocks_since_snapshot_head = 100 + args.measured_blocks
        trigger_extra_blocks = (-blocks_since_snapshot_head) % persistence_batch_blocks
        drain_end = measured_end + trigger_extra_blocks
        if trigger_extra_blocks:
            run_logged(
                bench_command(args.reth_bench, jwt, measured_end, drain_end, args.output / "persistence-trigger"),
                args.output / "persistence-trigger.log",
            )
        drain_parent = drain_end - 1
        run_logged(
            bench_command(args.reth_bench, jwt, drain_parent, drain_end, args.output / "persistence-drain"),
            args.output / "persistence-drain.log",
        )
        completion_ns = time.monotonic_ns()
        completion_epoch_ms = time.time_ns() / 1e6
        completion_wall = utc_now()
        elapsed = (completion_ns - start_ns) / 1e9
        injection_elapsed = (injection_ns - start_ns) / 1e9
        drain_elapsed = (completion_ns - injection_ns) / 1e9
        cpu_after = process_cpu_seconds(node_pid)
        sampler.stop()
        persistence_sampler.stop()
        os.killpg(iostat.pid, signal.SIGINT)
        try:
            iostat.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(iostat.pid, signal.SIGTERM)
            iostat.wait(timeout=10)
        iostat_log.close()

        after_text = scrape_metrics()
        (args.output / "metrics-after.prom").write_text(after_text)
        after = parse_prometheus(after_text)
        wrapper_exit = stop_process_group(node_wrapper)
        node_log.close()
        if wrapper_exit != 0:
            raise RuntimeError(f"node/profiler wrapper exited {wrapper_exit}")
        if args.profiler == "samply" and not profile_path.is_file():
            raise RuntimeError("samply profile was not written")

        reopen_log = (args.output / "persistence-reopen.log").open("wb")
        verifier = subprocess.Popen(
            node_command(
                args.reth,
                args.datadir,
                jwt,
                args.persistence_threshold,
                persistence_backpressure_threshold,
                metrics=False,
            ),
            stdout=reopen_log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            persisted_head = wait_rpc("http://127.0.0.1:8545", drain_end)
            persisted_block = rpc("http://127.0.0.1:8545", "eth_getBlockByNumber", [hex(measured_end), False])
        finally:
            verifier_exit = stop_process_group(verifier)
            reopen_log.close()
        expected = expected_block(args.corpus, measured_end)
        if verifier_exit != 0 or persisted_head != drain_end:
            raise RuntimeError("cold persistence reopen failed")
        if persisted_block["hash"].lower() != expected["hash"].lower() or persisted_block["stateRoot"].lower() != expected["stateRoot"].lower():
            raise RuntimeError("canonical hash/state-root mismatch")

        combined_path = args.output / "measured/combined_latency.csv"
        with combined_path.open(newline="") as stream:
            combined = list(csv.DictReader(stream))
        if len(combined) != args.measured_blocks:
            raise RuntimeError(f"measured block count {len(combined)}")
        block_numbers = [int(row["block_number"]) for row in combined]
        if block_numbers != list(range(MEASURED_START + 1, measured_end + 1)):
            raise RuntimeError("measured blocks are not the frozen contiguous range")
        persistence_wait = sum(int(row["persistence_wait"]) for row in combined) / 1e6
        new_payload = sum(int(row["new_payload_latency"]) for row in combined) / 1e6
        fcu = sum(int(row["fcu_latency"]) for row in combined) / 1e6
        total_gas = sum(int(row["gas_used"]) for row in combined)
        resource_path = args.output / "resource-samples.json"
        resource_path.write_text(json.dumps(sampler.rows, separators=(",", ":")) + "\n")
        persistence_samples_path = args.output / "production-metrics-samples.json"
        persistence_samples_path.write_text(
            json.dumps(persistence_sampler.rows, separators=(",", ":")) + "\n"
        )
        cpu_values = [float(row["cpu_pct"]) for row in sampler.rows]
        rss_values = [int(row["rss_bytes"]) for row in sampler.rows]
        state_counts = collections.Counter(str(row["state"]) for row in sampler.rows)
        wait_channel_counts = collections.Counter(str(row["wait_channel"]) for row in sampler.rows)
        sync_seconds = metric_base_delta(before, after, "reth_database_transaction_commit_sync_duration_seconds")
        sync_count = metric_base_count_delta(before, after, "reth_database_transaction_commit_sync_duration_seconds")
        disk = parse_iostat(iostat_path, elapsed)
        disk["mdbx_commit_sync_average_ms"] = (sync_seconds / sync_count * 1000) if sync_count else 0.0
        measurement_persistence_samples = [
            row for row in persistence_sampler.rows if row["phase"] == "measurement"
        ]
        sampled_backlogs = [
            float(row["reth_blockchain_tree_in_mem_state_num_blocks"])
            for row in measurement_persistence_samples
        ]
        sampled_backpressure = [
            float(row["reth_consensus_engine_beacon_backpressure_active"])
            for row in measurement_persistence_samples
        ]
        injection_backlog = int(
            injection_metrics.get("reth_blockchain_tree_in_mem_state_num_blocks", 0.0)
        )

        manifest = {
            "schema_version": "reth-fpga-oh165-profile-run/v2",
            "run_id": args.run_id,
            "valid": True,
            "host": {"system": platform.system(), "release": platform.mac_ver()[0], "machine": platform.machine(), "logical_cpus": os.cpu_count()},
            "profiler": args.profiler,
            "profiler_isolation": {"simultaneous_stack_profilers": 1 if args.profiler == "samply" else 0, "samply_rate_hz": 99 if args.profiler == "samply" else None},
            "frozen_input_guardrail": frozen_guard,
            "source_commit": frozen_guard["observed"]["source_commit"],
            "engine": {
                "persistence_threshold": args.persistence_threshold,
                "persistence_backpressure_threshold": persistence_backpressure_threshold,
                "memory_block_buffer_target": 0,
                "database_sync_mode": "durable",
                "expected_persistence_batch_blocks": persistence_batch_blocks,
            },
            "request_path": {
                "measurement_mode": args.measurement_request_mode,
                "engine_method": (
                    "engine_newPayload + engine_forkchoiceUpdated"
                    if args.measurement_request_mode == "production-standard"
                    else "reth_newPayload(wait_for_persistence=always)"
                ),
                "configured_block_interval_ms": args.block_interval_ms,
                "setup_and_warmup_mode": "forced-persistence (excluded)",
                "drain_mode": "forced-persistence (excluded)",
            },
            "clone_seconds": clone_seconds,
            "clone_source": str(clone_source),
            "prepared_datadir_reused": args.prepared_datadir is not None,
            "prepared_datadir_cold_start_head": start_head,
            "setup": {"from": HEAD + 1, "to": SETUP_END, "blocks": 80, "excluded": True},
            "warmup": {"from": SETUP_END + 1, "to": MEASURED_START, "blocks": 20, "excluded": True},
            "measurement": {"from": MEASURED_START + 1, "to": measured_end, "blocks": args.measured_blocks},
            "initial_forkchoice": initial_forkchoice,
            "boundaries": {
                "start_monotonic_ns": start_ns,
                "injection_end_monotonic_ns": injection_ns,
                "completion_monotonic_ns": completion_ns,
                "start_epoch_ms": start_epoch_ms,
                "injection_end_epoch_ms": injection_epoch_ms,
                "completion_epoch_ms": completion_epoch_ms,
                "start_utc": start_wall,
                "injection_end_utc": injection_wall,
                "completion_utc": completion_wall,
            },
            "production_persistence": {
                "sample_period_seconds": 1.0,
                "measurement_samples": len(measurement_persistence_samples),
                "in_memory_blocks_at_injection_end": injection_backlog,
                "max_sampled_in_memory_blocks": max(sampled_backlogs, default=0.0),
                "backpressure_active_samples": sum(value > 0 for value in sampled_backpressure),
                "backpressure_active_sample_ratio": (
                    sum(value > 0 for value in sampled_backpressure) / len(sampled_backpressure)
                    if sampled_backpressure else 0.0
                ),
                "backpressure_stall_seconds_during_injection": metric_base_delta(
                    before,
                    injection_metrics,
                    "reth_consensus_engine_beacon_backpressure_stall_duration",
                ),
                "backpressure_stall_count_during_injection": metric_base_count_delta(
                    before,
                    injection_metrics,
                    "reth_consensus_engine_beacon_backpressure_stall_duration",
                ),
                "save_blocks_seconds_during_injection": metric_base_delta(
                    before,
                    injection_metrics,
                    "reth_consensus_engine_persistence_save_blocks_duration_seconds",
                ),
                "save_blocks_count_during_injection": metric_base_count_delta(
                    before,
                    injection_metrics,
                    "reth_consensus_engine_persistence_save_blocks_duration_seconds",
                ),
            },
            "persistence_completion": {
                "verified": True,
                "cold_reopen_verified": True,
                "measured_block_verified": measured_end,
                "persisted_head": persisted_head,
                "drain_method": "reach persistence-trigger remainder, then duplicate final drain payload with wait-for-persistence always",
                "excluded_trigger_blocks": trigger_extra_blocks,
                "excluded_drain_payloads": trigger_extra_blocks + 1,
            },
            "wall_clock": {
                "e2e_seconds": elapsed,
                "injection_seconds": injection_elapsed,
                "excluded_durability_drain_seconds": drain_elapsed,
                "new_payload_seconds": new_payload,
                "fcu_seconds": fcu,
                "persistence_wait_seconds": persistence_wait,
                "persistence_wait_ratio": persistence_wait / elapsed,
                "unattributed_seconds": max(0.0, elapsed - new_payload - fcu - persistence_wait),
            },
            "cpu": {
                "core_seconds": max(0.0, cpu_after - cpu_before),
                "injection_core_seconds": max(0.0, cpu_injection - cpu_before),
                "injection_average_cores": max(0.0, cpu_injection - cpu_before) / injection_elapsed,
                "average_cores": max(0.0, cpu_after - cpu_before) / elapsed,
                "host_capacity_ratio": max(0.0, cpu_after - cpu_before) / elapsed / (os.cpu_count() or 1),
                "ps_average_pct": statistics.fmean(cpu_values),
                "ps_max_pct": max(cpu_values),
                "process_state_counts": dict(state_counts),
                "wait_channel_counts": dict(wait_channel_counts),
            },
            "rss": {"average_bytes": round(statistics.fmean(rss_values)), "max_bytes": max(rss_values)},
            "throughput": {
                "blocks_per_second": args.measured_blocks / injection_elapsed,
                "total_gas": total_gas,
                "mgas_per_second": total_gas / 1e6 / injection_elapsed,
                "denominator": "measurement injection wall clock; excluded durability drain is reported separately",
            },
            "disk": disk,
            "mdbx": {
                "transaction_open_seconds": metric_base_delta(before, after, "reth_database_transaction_open_duration_seconds"),
                "transaction_close_seconds": metric_base_delta(before, after, "reth_database_transaction_close_duration_seconds"),
                "commit_whole_seconds": metric_base_delta(before, after, "reth_database_transaction_commit_whole_duration_seconds"),
                "commit_write_flush_seconds": metric_base_delta(before, after, "reth_database_transaction_commit_write_duration_seconds"),
                "commit_sync_seconds": sync_seconds,
                "commit_sync_count": sync_count,
                "save_blocks_mdbx_seconds": metric_base_delta(before, after, "reth_storage_providers_database_save_blocks_mdbx"),
                "save_blocks_write_trie_updates_seconds": metric_base_delta(before, after, "reth_storage_providers_database_save_blocks_write_trie_updates"),
                "save_blocks_write_hashed_state_seconds": metric_base_delta(before, after, "reth_storage_providers_database_save_blocks_write_hashed_state"),
                "save_blocks_write_state_seconds": metric_base_delta(before, after, "reth_storage_providers_database_save_blocks_write_state"),
                "save_blocks_insert_block_seconds": metric_base_delta(before, after, "reth_storage_providers_database_save_blocks_insert_block"),
                "save_blocks_commit_mdbx_seconds": metric_base_delta(before, after, "reth_storage_providers_database_save_blocks_commit_mdbx"),
            },
            "trie": {
                "state_root_seconds": metric_base_delta(before, after, "reth_sync_block_validation_state_root_histogram"),
                "execution_seconds": metric_base_delta(before, after, "reth_sync_execution_execution_histogram"),
                "cursor_traversal_seconds": metric_base_delta(before, after, "reth_trie_cursor_overall_duration"),
                "hashed_cursor_seconds": metric_base_delta(before, after, "reth_trie_hashed_cursor_overall_duration"),
            },
            "canonical_correctness": {
                "block": measured_end,
                "expected_hash": expected["hash"].lower(),
                "actual_hash": persisted_block["hash"].lower(),
                "expected_state_root": expected["stateRoot"].lower(),
                "actual_state_root": persisted_block["stateRoot"].lower(),
            },
            "raw_evidence": {
                "combined_latency_sha256": sha256(combined_path),
                "metrics_before_sha256": sha256(args.output / "metrics-before.prom"),
                "metrics_injection_end_sha256": sha256(args.output / "metrics-injection-end.prom"),
                "metrics_after_sha256": sha256(args.output / "metrics-after.prom"),
                "iostat_sha256": sha256(iostat_path),
                "resource_samples_sha256": sha256(resource_path),
                "production_metrics_samples_sha256": sha256(persistence_samples_path),
                "samply_profile_sha256": sha256(profile_path) if profile_path.is_file() else None,
            },
        }
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(json.dumps({
            "run_id": args.run_id,
            "valid": True,
            "injection_seconds": injection_elapsed,
            "durability_drain_seconds": drain_elapsed,
            "e2e_seconds": elapsed,
            "output": str(args.output),
        }))
        return 0
    finally:
        if node_wrapper.poll() is None:
            stop_process_group(node_wrapper)
        node_log.close()
        if fixture.poll() is None:
            stop_process_group(fixture, timeout=10)
        fixture_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
