#!/usr/bin/env python3
"""Fail-closed CP-1 snapshot restore and short real Engine replay."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

HERE = Path(__file__).resolve().parent
CORPUS_DIR = HERE.parent / "cp1-corpus"
sys.path.insert(0, str(CORPUS_DIR))

from corpus import (  # noqa: E402
    EXPECTED_RETH_COMMIT,
    CorpusError,
    canonical_json,
    load_json,
    sha256_bytes,
    sha256_file,
    validate_lock,
    verify_reth_source,
)

MANIFEST_SCHEMA = "reth-fpga-cp1-replay-manifest/v1"
INVENTORY_SCHEMA = "reth-fpga-cp1-snapshot-inventory/v1"
REPORT_SCHEMA = "reth-fpga-cp1-correctness-report/v1"
EXPECTED_CHAIN_ID = 1
EXPECTED_CHAIN_NAME = "mainnet"
MINIMUM_PAYLOADS = 8
MAXIMUM_PAYLOADS = 32
SHA256_LENGTH = 64


class ReplayError(ValueError):
    """A deterministic snapshot or replay contract violation."""


def required(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    try:
        for key in path:
            current = current[key]
    except (KeyError, TypeError) as error:
        raise ReplayError(f"manifest is missing {'.'.join(path)}") from error
    return current


def resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def sha256_command(command: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, check=True, capture_output=True, timeout=30
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReplayError(f"cannot run provenance command {command!r}: {error}") from error
    return sha256_bytes(result.stdout)


def validate_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReplayError(f"{field} must be a lowercase SHA-256")
    return value


def validate_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = load_json(path)
    except CorpusError as error:
        raise ReplayError(str(error)) from error
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ReplayError(f"manifest schema must be {MANIFEST_SCHEMA}")
    base = path.parent

    contract_path = resolve(base, required(manifest, "contract", "path"))
    contract_sha256 = validate_sha256(
        required(manifest, "contract", "sha256"), "contract sha256"
    )
    if sha256_file(contract_path) != contract_sha256:
        raise ReplayError("experiment contract sha256 mismatch")
    contract = load_json(contract_path)
    software = required(contract, "software")
    source_path = resolve(base, required(manifest, "source", "path"))
    source_commit = required(manifest, "source", "commit")
    if (
        source_commit != EXPECTED_RETH_COMMIT
        or source_commit != required(software, "reth_commit")
        or source_commit != required(software, "reth_bench_commit")
    ):
        raise ReplayError("manifest Reth commit is not pinned to v2.0.0")
    try:
        verify_reth_source(source_path)
    except CorpusError as error:
        raise ReplayError(str(error)) from error
    cargo_lock = source_path / "Cargo.lock"
    cargo_lock_sha256 = validate_sha256(
        required(manifest, "source", "cargo_lock_sha256"), "Cargo.lock sha256"
    )
    if (
        cargo_lock_sha256 != required(software, "cargo_lock_sha256")
        or sha256_file(cargo_lock) != cargo_lock_sha256
    ):
        raise ReplayError("Cargo.lock sha256 mismatch")
    toolchain = required(manifest, "source", "rust_toolchain")
    if toolchain != required(software, "rust_version"):
        raise ReplayError("source rust_toolchain does not match experiment contract")
    rustc_hash = sha256_command(["rustc", f"+{toolchain}", "-vV"], source_path)
    if rustc_hash != validate_sha256(
        required(manifest, "source", "rustc_verbose_version_sha256"),
        "rustc verbose-version sha256",
    ):
        raise ReplayError("rustc verbose-version sha256 mismatch")
    for key in ("profile", "rustflags"):
        if required(manifest, "source", key) != software[
            "build_profile" if key == "profile" else key
        ]:
            raise ReplayError(f"source {key} does not match experiment contract")

    for name in ("reth", "reth_bench"):
        binary = required(manifest, "binaries", name)
        binary_path = resolve(base, required(binary, "path"))
        if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
            raise ReplayError(f"{name} binary is missing or not executable")
        if sha256_file(binary_path) != validate_sha256(
            required(binary, "sha256"), f"{name} binary sha256"
        ):
            raise ReplayError(f"{name} binary sha256 mismatch")
        contract_name = "reth_bench" if name == "reth_bench" else name
        if required(binary, "build_command") != required(
            software, "build_commands", contract_name
        ):
            raise ReplayError(f"{name} build command does not match experiment contract")

    corpus = required(manifest, "corpus")
    corpus_path = resolve(base, required(corpus, "path"))
    lock_path = resolve(base, required(corpus, "lock_path"))
    if sha256_file(lock_path) != validate_sha256(
        required(corpus, "lock_sha256"), "corpus lock sha256"
    ):
        raise ReplayError("corpus lock sha256 mismatch")
    try:
        lock, blocks = validate_lock(contract_path, corpus_path, lock_path)
    except CorpusError as error:
        raise ReplayError(str(error)) from error
    for manifest_key, lock_key in (
        ("artifact_sha256", "artifact_sha256"),
        ("ordered_block_hashes_sha256", "ordered_block_hashes_sha256"),
        ("fixture_server_sha256", "fixture_server_sha256"),
    ):
        value = validate_sha256(
            required(corpus, manifest_key), f"corpus {manifest_key}"
        )
        if value != lock["artifact"][lock_key]:
            raise ReplayError(f"corpus {manifest_key} mismatch")

    return manifest, validate_replay_contract(manifest, contract, blocks)


def validate_replay_contract(
    manifest: dict[str, Any],
    contract: dict[str, Any],
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if (
        required(manifest, "chain", "id") != EXPECTED_CHAIN_ID
        or required(manifest, "chain", "name") != EXPECTED_CHAIN_NAME
        or required(contract, "workload", "chain_id") != EXPECTED_CHAIN_ID
        or required(contract, "workload", "chain") != "ethereum-mainnet"
    ):
        raise ReplayError("chain must be Ethereum mainnet (id 1)")
    contract_snapshot = required(contract, "workload", "snapshot")
    if (
        required(manifest, "snapshot", "format") != required(contract_snapshot, "format")
        or required(manifest, "snapshot", "logical_identity")
        != required(contract_snapshot, "logical_identity")
    ):
        raise ReplayError("snapshot identity does not match the CP-0 workload contract")
    snapshot_head = contract["workload"]["snapshot_head"]
    anchor = required(manifest, "anchor")
    expected_anchor = {
        "number": snapshot_head["number"],
        "hash": snapshot_head["hash"],
        "state_root": snapshot_head["state_root"],
    }
    if anchor != expected_anchor:
        raise ReplayError("anchor does not exactly match the CP-0 snapshot head")
    replay = required(manifest, "replay")
    payloads = replay["to"] - replay["from"] + 1
    warmup = required(contract, "workload", "warmup")
    if (
        replay["from"] != anchor["number"] + 1
        or replay["from"] != required(warmup, "from")
        or replay["to"] > required(warmup, "to")
        or replay["payloads"] != payloads
        or payloads < MINIMUM_PAYLOADS
        or payloads > MAXIMUM_PAYLOADS
    ):
        raise ReplayError(
            "replay must be the first contiguous CP-0 warm-up slice with 8..32 payloads"
        )
    timeout_limits = {"node_ready": 300, "replay": 900, "process_stop": 60}
    for name, limit in timeout_limits.items():
        value = required(manifest, "timeouts_seconds", name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or value > limit
        ):
            raise ReplayError(f"timeouts_seconds.{name} must be within 1..{limit}")
    by_number = {int(block["number"], 16): block for block in blocks}
    actual_numbers = [int(block["number"], 16) for block in blocks]
    if actual_numbers != sorted(actual_numbers):
        raise ReplayError("locked corpus input order is not strictly ascending")
    expected_numbers = list(range(replay["from"], replay["to"] + 1))
    if any(number not in by_number for number in expected_numbers):
        raise ReplayError("replay slice is not fully present in the locked corpus")
    selected = [by_number[number] for number in expected_numbers]
    for index, block in enumerate(selected):
        if index and block["parentHash"].lower() != selected[index - 1]["hash"].lower():
            raise ReplayError(f"replay corpus order mismatch at block {expected_numbers[index]}")
    return selected


def load_inventory(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    snapshot = required(manifest, "snapshot")
    path = resolve(manifest_path.parent, required(snapshot, "inventory_path"))
    if sha256_file(path) != validate_sha256(
        required(snapshot, "inventory_sha256"), "snapshot inventory sha256"
    ):
        raise ReplayError("snapshot inventory sha256 mismatch")
    inventory = load_json(path)
    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise ReplayError(f"snapshot inventory schema must be {INVENTORY_SCHEMA}")
    if inventory.get("logical_identity") != required(snapshot, "logical_identity"):
        raise ReplayError("snapshot inventory logical identity mismatch")
    files = inventory.get("files")
    if not isinstance(files, list) or not files:
        raise ReplayError("snapshot inventory must contain at least one file")
    seen: set[str] = set()
    for entry in files:
        path_text = entry.get("path")
        pure = PurePosixPath(path_text) if isinstance(path_text, str) else PurePosixPath("/")
        if (
            not isinstance(path_text, str)
            or pure.is_absolute()
            or ".." in pure.parts
            or path_text in seen
            or not isinstance(entry.get("bytes"), int)
            or isinstance(entry.get("bytes"), bool)
            or entry["bytes"] < 0
        ):
            raise ReplayError(f"invalid snapshot inventory entry: {entry!r}")
        validate_sha256(entry.get("sha256"), f"snapshot inventory {path_text} sha256")
        seen.add(path_text)
    return inventory


def validate_snapshot_source(
    manifest_path: Path, manifest: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    snapshot = required(manifest, "snapshot")
    if required(snapshot, "format") != "reth-datadir-tar-zstd":
        raise ReplayError("snapshot format must be reth-datadir-tar-zstd")
    archive = resolve(manifest_path.parent, required(snapshot, "path"))
    if not archive.is_file():
        raise ReplayError("snapshot archive is missing")
    expected_bytes = required(snapshot, "bytes")
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes <= 0
        or archive.stat().st_size != expected_bytes
    ):
        raise ReplayError("snapshot byte size mismatch")
    if sha256_file(archive) != validate_sha256(
        required(snapshot, "artifact_sha256"), "snapshot artifact sha256"
    ):
        raise ReplayError("snapshot artifact sha256 mismatch")
    required_paths = required(snapshot, "required_paths")
    if not isinstance(required_paths, list) or not required_paths:
        raise ReplayError("snapshot required_paths must be a non-empty list")
    for path_text in required_paths:
        pure = PurePosixPath(path_text) if isinstance(path_text, str) else PurePosixPath("/")
        if not isinstance(path_text, str) or pure.is_absolute() or ".." in pure.parts:
            raise ReplayError(f"invalid snapshot required path: {path_text!r}")
    inventory = load_inventory(manifest_path, manifest)
    return archive, inventory


def preflight(
    manifest_path: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    Path,
    dict[str, Any],
]:
    manifest, selected = validate_manifest(manifest_path)
    archive, inventory = validate_snapshot_source(manifest_path, manifest)
    return manifest, selected, archive, inventory


def validate_archive_paths(archive: Path) -> None:
    try:
        result = subprocess.run(
            ["tar", "--zstd", "-tf", str(archive)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReplayError(f"cannot list zstd snapshot archive: {error}") from error
    for name in result.stdout.splitlines():
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts:
            raise ReplayError(f"unsafe path in snapshot archive: {name}")


def validate_restored_tree(
    datadir: Path, inventory: dict[str, Any], required_paths: list[str]
) -> None:
    actual: dict[str, Path] = {}
    for path in datadir.rglob("*"):
        if path.is_symlink():
            raise ReplayError(f"restored snapshot contains symlink: {path.relative_to(datadir)}")
        if path.is_file():
            actual[path.relative_to(datadir).as_posix()] = path
        elif not path.is_dir():
            raise ReplayError(
                f"restored snapshot contains unsupported entry: {path.relative_to(datadir)}"
            )
    expected = {entry["path"]: entry for entry in inventory["files"]}
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ReplayError(
            f"restored snapshot inventory mismatch: "
            f"missing={missing[:3]} extra={extra[:3]}"
        )
    for relative, entry in expected.items():
        path = actual[relative]
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise ReplayError(f"restored snapshot file mismatch: {relative}")
    for relative in required_paths:
        path = datadir / relative
        if not path.exists():
            raise ReplayError(f"restored snapshot required path is missing: {relative}")


def restore_snapshot(
    manifest: dict[str, Any],
    archive: Path,
    inventory: dict[str, Any],
    destination: Path,
) -> str:
    if destination.exists():
        raise ReplayError(f"restore destination already exists: {destination}")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    required_free = required(manifest, "snapshot", "minimum_free_bytes_after_restore")
    if (
        not isinstance(required_free, int)
        or isinstance(required_free, bool)
        or required_free < 0
    ):
        raise ReplayError("snapshot minimum_free_bytes_after_restore must be non-negative")
    unpacked_bytes = sum(entry["bytes"] for entry in inventory["files"])
    if shutil.disk_usage(parent).free < unpacked_bytes + required_free:
        raise ReplayError("insufficient free space for snapshot restore")
    validate_archive_paths(archive)
    source_hash_before = sha256_file(archive)
    destination.mkdir(mode=0o700)
    try:
        subprocess.run(
            [
                "tar",
                "--zstd",
                "-xf",
                str(archive),
                "-C",
                str(destination),
                "--no-same-owner",
                "--no-same-permissions",
            ],
            check=True,
            timeout=1800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReplayError(f"snapshot extraction failed: {error}") from error
    validate_restored_tree(
        destination, inventory, required(manifest, "snapshot", "required_paths")
    )
    if sha256_file(archive) != source_hash_before:
        raise ReplayError("snapshot source changed during restore")
    return source_hash_before


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def rpc(url: str, method: str, params: list[Any], timeout: float = 5) -> Any:
    body = canonical_json({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ReplayError(f"RPC {method} failed: {error}") from error
    if "error" in value:
        raise ReplayError(f"RPC {method} returned error: {value['error']}")
    return value.get("result")


class Capture:
    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def append(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        method = request.get("method", "")
        if not (
            isinstance(method, str)
            and (
                method.startswith("engine_newPayload")
                or method.startswith("engine_forkchoiceUpdated")
            )
        ):
            return
        params = request.get("params", [])
        result = response.get("result", {})
        if method.startswith("engine_newPayload"):
            payload = params[0] if params and isinstance(params[0], dict) else {}
            record = {
                "kind": "new_payload",
                "method": method,
                "block_number": int(payload.get("blockNumber", "0x0"), 16),
                "block_hash": str(payload.get("blockHash", "")).lower(),
                "status": result.get("status"),
                "latest_valid_hash": str(result.get("latestValidHash", "")).lower(),
            }
        else:
            state = params[0] if params and isinstance(params[0], dict) else {}
            status = result.get("payloadStatus", {}) if isinstance(result, dict) else {}
            record = {
                "kind": "forkchoice_updated",
                "method": method,
                "head_block_hash": str(state.get("headBlockHash", "")).lower(),
                "status": status.get("status"),
                "latest_valid_hash": str(status.get("latestValidHash", "")).lower(),
            }
        with self.lock:
            self.records.append(record)


def proxy_handler(capture: Capture) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                request_value = json.loads(raw)
                connection = http.client.HTTPConnection(
                    "127.0.0.1", capture.upstream_port, timeout=30
                )
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "content-length"}
                }
                connection.request("POST", "/", body=raw, headers=headers)
                response = connection.getresponse()
                response_raw = response.read()
                response_value = json.loads(response_raw)
                capture.append(request_value, response_value)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_raw)))
                self.end_headers()
                self.wfile.write(response_raw)
            except Exception as error:  # pragma: no cover - network failure path
                self.send_error(502, str(error))

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def wait_for_anchor(url: str, anchor: dict[str, Any], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            block = rpc(url, "eth_getBlockByNumber", [hex(anchor["number"]), False])
            if block is None:
                raise ReplayError("anchor block is absent")
            actual = {
                "number": int(block["number"], 16),
                "hash": block["hash"].lower(),
                "state_root": block["stateRoot"].lower(),
            }
            expected = {
                "number": anchor["number"],
                "hash": anchor["hash"].lower(),
                "state_root": anchor["state_root"].lower(),
            }
            if actual == expected:
                return
            raise ReplayError(f"restored anchor mismatch: expected={expected} actual={actual}")
        except ReplayError as error:
            if str(error).startswith("restored anchor mismatch"):
                raise
            last_error = str(error)
            time.sleep(0.5)
    raise ReplayError(f"node did not expose the expected anchor: {last_error}")


def terminate(process: subprocess.Popen[bytes] | None, timeout: int) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def preserve_bounded_logs(logs: Path, destination: Path, limit: int = 1024 * 1024) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for source in logs.glob("*.log"):
        data = source.read_bytes()
        if len(data) > limit:
            data = b"[truncated to final 1048576 bytes]\n" + data[-limit:]
        (destination / source.name).write_bytes(data)


def stable_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": report["schema_version"],
        "manifest_sha256": report["manifest_sha256"],
        "snapshot_artifact_sha256": report["snapshot_artifact_sha256"],
        "corpus_artifact_sha256": report["corpus_artifact_sha256"],
        "anchor": report["anchor"],
        "replay": report["replay"],
        "blocks": report["blocks"],
    }


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    report["correctness_digest"] = sha256_bytes(canonical_json(stable_payload(report)))
    return report


def build_block_records(
    selected: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    node_rpc_url: str,
) -> list[dict[str, Any]]:
    if len(captures) != len(selected) * 2:
        raise ReplayError(
            f"Engine capture count mismatch: expected={len(selected) * 2} actual={len(captures)}"
        )
    records: list[dict[str, Any]] = []
    for index, block in enumerate(selected):
        number = int(block["number"], 16)
        block_hash = block["hash"].lower()
        np, fc = captures[index * 2 : index * 2 + 2]
        if np.get("kind") != "new_payload" or fc.get("kind") != "forkchoice_updated":
            raise ReplayError(f"unexpected Engine call order at block {number}")
        if np.get("block_number") != number or fc.get("head_block_hash") != block_hash:
            raise ReplayError(f"Engine sequence mismatch at block {number}")
        if np["block_hash"] != block_hash:
            raise ReplayError(f"Engine input block hash mismatch at block {number}")
        if np["status"] != "VALID" or fc["status"] != "VALID":
            raise ReplayError(
                f"unexpected Engine status at block {number}: "
                f"{np['status']}/{fc['status']}"
            )
        if np["latest_valid_hash"] != block_hash or fc["latest_valid_hash"] != block_hash:
            raise ReplayError(f"Engine latestValidHash mismatch at block {number}")
        observed = rpc(node_rpc_url, "eth_getBlockByNumber", [hex(number), False])
        if observed is None:
            raise ReplayError(f"canonical output block is missing at block {number}")
        output_hash = observed["hash"].lower()
        output_root = observed["stateRoot"].lower()
        if output_hash != block_hash or output_root != block["stateRoot"].lower():
            raise ReplayError(f"canonical output/state-root mismatch at block {number}")
        records.append(
            {
                "block_number": number,
                "input_block_hash": block_hash,
                "input_state_root": block["stateRoot"].lower(),
                "new_payload_method": np["method"],
                "new_payload_status": np["status"],
                "new_payload_latest_valid_hash": np["latest_valid_hash"],
                "forkchoice_method": fc["method"],
                "forkchoice_status": fc["status"],
                "forkchoice_latest_valid_hash": fc["latest_valid_hash"],
                "output_block_hash": output_hash,
                "output_state_root": output_root,
            }
        )
    return records


def validate_read_only_sources_unchanged(
    manifest_path: Path, manifest: dict[str, Any], manifest_sha256: str
) -> None:
    base = manifest_path.parent
    paths_and_hashes = (
        (manifest_path, manifest_sha256, "run manifest"),
        (
            resolve(base, required(manifest, "contract", "path")),
            required(manifest, "contract", "sha256"),
            "experiment contract",
        ),
        (
            resolve(base, required(manifest, "corpus", "path")),
            required(manifest, "corpus", "artifact_sha256"),
            "locked corpus",
        ),
        (
            resolve(base, required(manifest, "corpus", "lock_path")),
            required(manifest, "corpus", "lock_sha256"),
            "corpus lock",
        ),
        (
            resolve(base, required(manifest, "snapshot", "path")),
            required(manifest, "snapshot", "artifact_sha256"),
            "snapshot archive",
        ),
        (
            resolve(base, required(manifest, "snapshot", "inventory_path")),
            required(manifest, "snapshot", "inventory_sha256"),
            "snapshot inventory",
        ),
        (
            resolve(base, required(manifest, "source", "path")) / "Cargo.lock",
            required(manifest, "source", "cargo_lock_sha256"),
            "pinned Cargo.lock",
        ),
        (
            resolve(base, required(manifest, "binaries", "reth", "path")),
            required(manifest, "binaries", "reth", "sha256"),
            "pinned reth binary",
        ),
        (
            resolve(base, required(manifest, "binaries", "reth_bench", "path")),
            required(manifest, "binaries", "reth_bench", "sha256"),
            "pinned reth-bench binary",
        ),
    )
    for path, expected_hash, label in paths_and_hashes:
        if sha256_file(path) != expected_hash:
            raise ReplayError(f"{label} changed during correctness replay")


def run_once(manifest_path: Path, report_path: Path, work_root: Path, keep: bool) -> dict[str, Any]:
    manifest_sha256 = sha256_file(manifest_path)
    manifest, selected, archive, inventory = preflight(manifest_path)
    if sha256_file(manifest_path) != manifest_sha256:
        raise ReplayError("run manifest changed during preflight validation")
    log_destination = report_path.with_suffix(".logs")
    if report_path.exists() or log_destination.exists():
        raise ReplayError("correctness report or log destination already exists")
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="cp1-correctness-", dir=work_root))
    datadir = run_dir / "datadir"
    logs = run_dir / "logs"
    logs.mkdir()
    node_process: subprocess.Popen[bytes] | None = None
    fixture_process: subprocess.Popen[bytes] | None = None
    proxy: ThreadingHTTPServer | None = None
    try:
        snapshot_hash = restore_snapshot(manifest, archive, inventory, datadir)
        jwt_path = run_dir / "jwt.hex"
        jwt_path.write_text(os.urandom(32).hex(), encoding="ascii")
        os.chmod(jwt_path, 0o600)
        node_http_port, node_auth_port, fixture_port, proxy_port = (free_port() for _ in range(4))
        base = manifest_path.parent
        reth = resolve(base, manifest["binaries"]["reth"]["path"])
        reth_bench = resolve(base, manifest["binaries"]["reth_bench"]["path"])
        corpus_path = resolve(base, manifest["corpus"]["path"])
        lock_path = resolve(base, manifest["corpus"]["lock_path"])
        contract_path = resolve(base, manifest["contract"]["path"])
        node_command = [
            str(reth),
            "node",
            "--chain",
            manifest["chain"]["name"],
            "--datadir",
            str(datadir),
            "--http",
            "--http.addr",
            "127.0.0.1",
            "--http.port",
            str(node_http_port),
            "--http.api",
            "eth",
            "--authrpc.addr",
            "127.0.0.1",
            "--authrpc.port",
            str(node_auth_port),
            "--authrpc.jwtsecret",
            str(jwt_path),
            "--disable-discovery",
            "--trusted-only",
            "--port",
            "0",
        ]
        fixture_command = [
            sys.executable,
            str(CORPUS_DIR / "fixture_server.py"),
            "--contract",
            str(contract_path),
            "--corpus",
            str(corpus_path),
            "--lock",
            str(lock_path),
            "--reth-source",
            str(resolve(base, manifest["source"]["path"])),
            "--port",
            str(fixture_port),
        ]
        with (logs / "node.log").open("wb") as node_log, (
            logs / "fixture.log"
        ).open("wb") as fixture_log:
            node_process = subprocess.Popen(node_command, stdout=node_log, stderr=subprocess.STDOUT)
            wait_for_anchor(
                f"http://127.0.0.1:{node_http_port}",
                manifest["anchor"],
                manifest["timeouts_seconds"]["node_ready"],
            )
            fixture_process = subprocess.Popen(
                fixture_command, stdout=fixture_log, stderr=subprocess.STDOUT
            )
            wait_for_anchor(
                f"http://127.0.0.1:{fixture_port}",
                manifest["anchor"],
                manifest["timeouts_seconds"]["node_ready"],
            )
            capture = Capture(node_auth_port)
            proxy = ThreadingHTTPServer(("127.0.0.1", proxy_port), proxy_handler(capture))
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            bench_output = run_dir / "reth-bench"
            bench_command = [
                str(reth_bench),
                "new-payload-fcu",
                "--rpc-url",
                f"http://127.0.0.1:{fixture_port}",
                "--engine-rpc-url",
                f"http://127.0.0.1:{proxy_port}",
                "--jwt-secret",
                str(jwt_path),
                "--rpc-block-fetch-retries",
                "0",
                "--from",
                str(manifest["anchor"]["number"]),
                "--to",
                str(manifest["replay"]["to"]),
                "--output",
                str(bench_output),
            ]
            with (logs / "reth-bench.log").open("wb") as bench_log:
                try:
                    subprocess.run(
                        bench_command,
                        check=True,
                        stdout=bench_log,
                        stderr=subprocess.STDOUT,
                        timeout=manifest["timeouts_seconds"]["replay"],
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                    raise ReplayError(f"reth-bench correctness replay failed: {error}") from error
            blocks = build_block_records(
                selected, capture.records, f"http://127.0.0.1:{node_http_port}"
            )
            validate_read_only_sources_unchanged(
                manifest_path, manifest, manifest_sha256
            )
        report = finalize_report(
            {
                "schema_version": REPORT_SCHEMA,
                "manifest_sha256": manifest_sha256,
                "snapshot_artifact_sha256": snapshot_hash,
                "corpus_artifact_sha256": manifest["corpus"]["artifact_sha256"],
                "anchor": manifest["anchor"],
                "replay": manifest["replay"],
                "blocks": blocks,
                "result": "PASS",
            }
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(canonical_json(report) + b"\n")
        return report
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        timeout = manifest.get("timeouts_seconds", {}).get("process_stop", 20)
        terminate(fixture_process, timeout)
        terminate(node_process, timeout)
        preserve_bounded_logs(logs, log_destination)
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)


def compare_reports(first_path: Path, second_path: Path) -> None:
    first = load_json(first_path)
    second = load_json(second_path)
    for report in (first, second):
        if report.get("schema_version") != REPORT_SCHEMA or report.get("result") != "PASS":
            raise ReplayError("both correctness reports must be PASS reports")
        expected = sha256_bytes(canonical_json(stable_payload(report)))
        if report.get("correctness_digest") != expected:
            raise ReplayError("correctness report digest is invalid")
    if stable_payload(first) != stable_payload(second):
        raise ReplayError("independent correctness reports differ")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--report", type=Path, required=True)
    run_parser.add_argument("--work-root", type=Path, required=True)
    run_parser.add_argument("--keep-workdir", action="store_true")
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            preflight(args.manifest)
        elif args.command == "run":
            run_once(args.manifest, args.report, args.work_root, args.keep_workdir)
        else:
            compare_reports(args.first, args.second)
    except (ReplayError, CorpusError, OSError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
