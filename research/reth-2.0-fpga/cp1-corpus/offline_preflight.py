#!/usr/bin/env python3
"""Capture pinned reth-bench block requests against the offline fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from corpus import CorpusError, validate_lock, verify_reth_source
from fixture_server import Fixture, make_server

CHECK_BLOCKS = (20999936, 20999999, 21000000, 21000099, 21000100, 21000599)
CLIENT_BLOCK = 21000001


class EngineStopHandler(BaseHTTPRequestHandler):
    """Terminate the client only after its fixture block prefetch completes."""

    methods: list[str] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
            request = json.loads(self.rfile.read(length))
            request_id = request.get("id")
            method = request.get("method")
            if isinstance(method, str):
                self.methods.append(method)
        except (ValueError, json.JSONDecodeError):
            request_id = None
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32099,
                    "message": "offline preflight stops before payload execution",
                },
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def source_semantics(source: Path) -> str:
    files = [
        source / "bin/reth-bench/src/bench/context.rs",
        source / "bin/reth-bench/src/bench/new_payload_fcu.rs",
    ]
    combined = b""
    for path in files:
        try:
            combined += path.read_bytes()
        except OSError as error:
            raise CorpusError(f"cannot read pinned reth-bench source {path}: {error}") from error
    for token in (b"BlockNumberOrTag::Latest", b".full()", b"saturating_sub(32)", b"saturating_sub(64)"):
        if token not in combined:
            raise CorpusError(f"pinned request semantics token missing: {token.decode()}")
    return hashlib.sha256(combined).hexdigest()


def verify_binary(binary: Path) -> None:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CorpusError(f"reth-bench binary is not executable: {binary}")
    try:
        result = subprocess.run(
            [str(binary), "new-payload-fcu", "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise CorpusError(f"cannot inspect reth-bench new-payload-fcu: {error}") from error
    for flag in ("--rpc-url", "--from", "--to", "--rpc-block-fetch-retries"):
        if flag not in result.stdout:
            raise CorpusError(f"reth-bench help is missing required flag {flag}")


def rpc(url: str, request_id: int, selector: str, full: bool) -> dict[str, Any]:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "eth_getBlockByNumber",
            "params": [selector, full],
        },
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.load(response)
    if "error" in value or not isinstance(value.get("result"), dict):
        raise CorpusError(f"offline request failed for {selector}: {value.get('error')}")
    return value["result"]


def capture(url: str, lock: dict[str, Any]) -> tuple[int, str]:
    responses = []
    latest = rpc(url, len(responses), "latest", True)
    responses.append(latest)
    if int(latest["number"], 16) != lock["range"]["to"]:
        raise CorpusError("latest did not resolve to the locked corpus end")
    for number in CHECK_BLOCKS:
        block = rpc(url, len(responses), hex(number), True)
        responses.append(block)
        identity = lock["boundary_identity"].get(str(number), {})
        for field, expected in identity.items():
            if block.get(field, "").lower() != expected:
                raise CorpusError(f"boundary response mismatch at block {number}: {field}")
    for number in (21000000, 21000100, 21000599):
        for lookback in (32, 64):
            block = rpc(url, len(responses), hex(number - lookback), False)
            responses.append(block)
            if int(block["number"], 16) != number - lookback:
                raise CorpusError(f"N-{lookback} lookup mismatch for block {number}")
    digest = hashlib.sha256(
        json.dumps(responses, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return len(responses), digest


def capture_fixture(
    blocks: list[dict[str, Any]], lock: dict[str, Any]
) -> tuple[int, str]:
    fixture = Fixture(blocks)
    server = make_server(fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return capture(f"http://127.0.0.1:{server.server_port}", lock)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def capture_client(
    binary: Path, fixture: Fixture, fixture_url: str, engine_url: str, jwt_path: Path
) -> tuple[tuple[str, ...], str]:
    EngineStopHandler.methods = []
    result = subprocess.run(
        [
            str(binary),
            "new-payload-fcu",
            "--rpc-url",
            fixture_url,
            "--from",
            str(CLIENT_BLOCK - 1),
            "--to",
            str(CLIENT_BLOCK),
            "--engine-rpc-url",
            engine_url,
            "--jwt-secret",
            str(jwt_path),
            "--rpc-block-fetch-retries",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        raise CorpusError("reth-bench unexpectedly passed the intentional Engine API stop")
    expected = {
        (hex(CLIENT_BLOCK - 32), False),
        (hex(CLIENT_BLOCK - 64), False),
    }
    missing = expected.difference(fixture.requests)
    if missing:
        raise CorpusError(f"reth-bench did not request required lookbacks: {sorted(missing)}")
    if not EngineStopHandler.methods:
        raise CorpusError("reth-bench did not reach the intentional Engine API stop")
    return tuple(sorted(selector for selector, _ in expected)), EngineStopHandler.methods[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reth-bench", required=True, type=Path)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "experiment-contract/v1/manifest.json",
    )
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument(
        "--reth-source",
        type=Path,
        default=os.environ.get("RETH_V2_SOURCE"),
        required="RETH_V2_SOURCE" not in os.environ,
    )
    args = parser.parse_args()
    try:
        verify_reth_source(args.reth_source)
        semantics_sha256 = source_semantics(args.reth_source)
        verify_binary(args.reth_bench)
        lock, blocks = validate_lock(args.contract, args.corpus, args.lock)
        fixture = Fixture(blocks)
        server = make_server(fixture)
        engine = ThreadingHTTPServer(("127.0.0.1", 0), EngineStopHandler)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True),
            threading.Thread(target=engine.serve_forever, daemon=True),
        ]
        for thread in threads:
            thread.start()
        with tempfile.TemporaryDirectory() as temporary:
            jwt_path = Path(temporary) / "jwt.hex"
            jwt_path.write_text("00" * 32, encoding="ascii")
            try:
                fixture_url = f"http://127.0.0.1:{server.server_port}"
                requests, response_sha256 = capture(fixture_url, lock)
                lookbacks, engine_method = capture_client(
                    args.reth_bench,
                    fixture,
                    fixture_url,
                    f"http://127.0.0.1:{engine.server_port}",
                    jwt_path,
                )
            finally:
                server.shutdown()
                engine.shutdown()
                server.server_close()
                engine.server_close()
                for thread in threads:
                    thread.join()
        second_requests, second_response_sha256 = capture_fixture(blocks, lock)
        if (requests, response_sha256) != (second_requests, second_response_sha256):
            raise CorpusError("offline second-run responses are not deterministic")
    except (CorpusError, OSError) as error:
        parser.error(str(error))
    print(
        f"OK offline reth-bench request capture: {requests} requests/run, "
        f"client_lookbacks={','.join(lookbacks)}, engine_stop={engine_method}, "
        f"response_sha256={response_sha256}, "
        f"source_semantics_sha256={semantics_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
