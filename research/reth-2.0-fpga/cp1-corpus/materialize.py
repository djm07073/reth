#!/usr/bin/env python3
"""One-time deterministic materializer for the locked CP-1 block corpus."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from corpus import (
    CorpusError,
    build_lock,
    canonical_json,
    contract_values,
    load_json,
    read_corpus,
    verify_reth_source,
    write_canonical,
)


def rpc(url: str, method: str, params: list[Any], request_id: int) -> Any:
    body = canonical_json(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "reth-cp1-materializer/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise CorpusError(f"upstream JSON-RPC request failed: {error}") from error
    if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
        raise CorpusError(f"upstream JSON-RPC returned an invalid response for request {request_id}")
    return payload["result"]


def materialize(
    contract_path: Path,
    corpus_path: Path,
    lock_path: Path,
    upstream: str,
) -> dict[str, Any]:
    values = contract_values(load_json(contract_path))
    chain_id = rpc(upstream, "eth_chainId", [], 1)
    if chain_id != hex(values["chain_id"]):
        raise CorpusError(f"upstream chain mismatch: expected 0x1, got {chain_id}")

    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = corpus_path.with_name(f".{corpus_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            for number in range(values["from"], values["to"] + 1):
                block = rpc(upstream, "eth_getBlockByNumber", [hex(number), True], number)
                if not isinstance(block, dict):
                    raise CorpusError(f"upstream has no full block {number}")
                handle.write(canonical_json(block) + b"\n")
                if (number - values["from"] + 1) % 50 == 0:
                    print(f"fetched {number - values['from'] + 1}/664", flush=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, corpus_path)
    finally:
        if temporary.exists():
            temporary.unlink()

    blocks = read_corpus(corpus_path)
    lock = build_lock(corpus_path, blocks, values)
    write_canonical(lock_path, lock)
    return lock


def outside_repository(path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    try:
        path.resolve().relative_to(repository)
    except ValueError:
        return
    raise CorpusError(f"generated corpus must be outside the repository: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--upstream", required=True, help="one-time trusted mainnet archive RPC URL")
    parser.add_argument(
        "--reth-source",
        type=Path,
        default=os.environ.get("RETH_V2_SOURCE"),
        required="RETH_V2_SOURCE" not in os.environ,
    )
    args = parser.parse_args()
    try:
        verify_reth_source(args.reth_source)
        outside_repository(args.corpus)
        lock = materialize(args.contract, args.corpus, args.lock, args.upstream)
    except CorpusError as error:
        parser.error(str(error))
    print(
        f"OK {lock['canonical_identity']}: {lock['range']['blocks']} blocks, "
        f"{lock['artifact']['bytes']} bytes, sha256={lock['artifact']['artifact_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
