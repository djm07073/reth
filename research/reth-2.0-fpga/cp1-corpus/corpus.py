#!/usr/bin/env python3
"""Shared deterministic corpus and lock helpers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "reth-fpga-cp1-corpus-lock/v1"
EXPECTED_RETH_COMMIT = "eb4c15e5e36d8776d46629beae4c0a69af7ab04f"
SERVER_FILE = Path(__file__).with_name("fixture_server.py")


class CorpusError(ValueError):
    """A deterministic corpus contract violation."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise CorpusError(f"{path} must contain a JSON object")
    return value


def contract_values(contract: dict[str, Any]) -> dict[str, Any]:
    try:
        software = contract["software"]
        workload = contract["workload"]
        source = workload["payload_source"]
        values = {
            "contract_id": contract["contract_id"],
            "reth_commit": software["reth_commit"],
            "reth_bench_commit": software["reth_bench_commit"],
            "chain_id": workload["chain_id"],
            "from": source["from"],
            "to": source["to"],
            "canonical_identity": source["canonical_identity"],
            "boundaries": {
                str(workload["snapshot_head"]["number"]): {
                    "hash": workload["snapshot_head"]["hash"],
                    "stateRoot": workload["snapshot_head"]["state_root"],
                },
                str(workload["warmup"]["from"]): {
                    "hash": workload["warmup"]["first_block_hash"],
                },
                str(workload["warmup"]["to"]): {
                    "hash": workload["warmup"]["last_block_hash"],
                },
                str(workload["measurement"]["from"]): {
                    "hash": workload["measurement"]["first_block_hash"],
                },
                str(workload["measurement"]["to"]): {
                    "hash": workload["measurement"]["last_block_hash"],
                    "stateRoot": workload["measurement"]["last_state_root"],
                },
            },
        }
    except (KeyError, TypeError) as error:
        raise CorpusError(f"contract is missing required CP-1 value: {error}") from error
    if values["reth_commit"] != EXPECTED_RETH_COMMIT:
        raise CorpusError(f"contract reth commit is not pinned to {EXPECTED_RETH_COMMIT}")
    if values["reth_bench_commit"] != EXPECTED_RETH_COMMIT:
        raise CorpusError(f"contract reth-bench commit is not pinned to {EXPECTED_RETH_COMMIT}")
    if values["to"] - values["from"] + 1 != 664:
        raise CorpusError("contract corpus range must contain exactly 664 blocks")
    return values


def verify_reth_source(source: Path, expected: str = EXPECTED_RETH_COMMIT) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise CorpusError(f"cannot verify pinned Reth checkout {source}: {error}") from error
    actual = result.stdout.strip()
    if actual != expected:
        raise CorpusError(f"Reth checkout mismatch: expected {expected}, got {actual}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CorpusError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def parse_quantity(value: Any) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise CorpusError("block number must be a canonical 0x quantity")
    digits = value[2:]
    if not digits or (len(digits) > 1 and digits[0] == "0"):
        raise CorpusError("block number must be a canonical 0x quantity")
    if any(character not in "0123456789abcdef" for character in digits):
        raise CorpusError("block number must be a lowercase canonical 0x quantity")
    return int(digits, 16)


def block_number(block: dict[str, Any]) -> int:
    try:
        return parse_quantity(block["number"])
    except KeyError as error:
        raise CorpusError("block is missing number") from error


def read_corpus(path: Path) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.endswith(b"\n"):
                    raise CorpusError(f"corpus line {line_number} has no trailing newline")
                try:
                    block = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise CorpusError(f"invalid JSON on corpus line {line_number}: {error}") from error
                if not isinstance(block, dict):
                    raise CorpusError(f"corpus line {line_number} is not an object")
                if raw_line != canonical_json(block) + b"\n":
                    raise CorpusError(f"corpus line {line_number} is not canonical JSON")
                blocks.append(block)
    except OSError as error:
        raise CorpusError(f"cannot read corpus {path}: {error}") from error
    return blocks


def ordered_hashes_sha256(blocks: Iterable[dict[str, Any]]) -> str:
    hashes = []
    for block in blocks:
        block_hash = block.get("hash")
        if not isinstance(block_hash, str) or len(block_hash) != 66 or not block_hash.startswith("0x"):
            raise CorpusError("every block must have a 32-byte hash")
        hashes.append(block_hash.lower())
    return sha256_bytes(("".join(f"{value}\n" for value in hashes)).encode())


def validate_blocks(
    blocks: list[dict[str, Any]], values: dict[str, Any]
) -> dict[str, dict[str, str]]:
    expected_numbers = list(range(values["from"], values["to"] + 1))
    actual_numbers = [block_number(block) for block in blocks]
    if actual_numbers != expected_numbers:
        raise CorpusError(
            f"corpus block ordering/range mismatch: expected {values['from']}..{values['to']}"
        )
    for index, block in enumerate(blocks):
        required = ("hash", "parentHash", "stateRoot", "transactions")
        missing = [field for field in required if field not in block]
        if missing:
            raise CorpusError(f"block {actual_numbers[index]} missing fields: {', '.join(missing)}")
        if not isinstance(block["transactions"], list):
            raise CorpusError(f"block {actual_numbers[index]} transactions must be a list")
        if index and block["parentHash"].lower() != blocks[index - 1]["hash"].lower():
            raise CorpusError(f"parent linkage mismatch at block {actual_numbers[index]}")

    frozen: dict[str, dict[str, str]] = {}
    by_number = {block_number(block): block for block in blocks}
    for number_text, fields in values["boundaries"].items():
        number = int(number_text)
        block = by_number[number]
        frozen[number_text] = {}
        for field, expected in fields.items():
            actual = block.get(field)
            if not isinstance(actual, str) or actual.lower() != expected.lower():
                raise CorpusError(
                    f"frozen {field} mismatch at block {number}: expected {expected}, got {actual}"
                )
            frozen[number_text][field] = actual.lower()
    first = blocks[0]
    frozen[str(values["from"])] = {
        "hash": first["hash"].lower(),
        "stateRoot": first["stateRoot"].lower(),
    }
    return dict(sorted(frozen.items(), key=lambda item: int(item[0])))


def build_lock(
    corpus_path: Path,
    blocks: list[dict[str, Any]],
    values: dict[str, Any],
) -> dict[str, Any]:
    boundaries = validate_blocks(blocks, values)
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": values["contract_id"],
        "canonical_identity": values["canonical_identity"],
        "reth_commit": values["reth_commit"],
        "reth_bench_commit": values["reth_bench_commit"],
        "chain_id": values["chain_id"],
        "range": {
            "from": values["from"],
            "to": values["to"],
            "blocks": len(blocks),
        },
        "artifact": {
            "format": "canonical-jsonl-full-transaction-blocks/v1",
            "artifact_sha256": sha256_file(corpus_path),
            "ordered_block_hashes_sha256": ordered_hashes_sha256(blocks),
            "fixture_server_sha256": sha256_file(SERVER_FILE),
            "bytes": corpus_path.stat().st_size,
        },
        "boundary_identity": boundaries,
    }


def validate_lock(
    contract_path: Path, corpus_path: Path, lock_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contract = load_json(contract_path)
    values = contract_values(contract)
    blocks = read_corpus(corpus_path)
    expected = build_lock(corpus_path, blocks, values)
    actual = load_json(lock_path)
    if actual != expected:
        raise CorpusError("corpus-lock.json does not exactly match corpus, contract, and server")
    return actual, blocks


def write_canonical(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(canonical_json(value) + b"\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
