#!/usr/bin/env python3
"""Read-only, loopback-only JSON-RPC server for a validated CP-1 corpus."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from corpus import CorpusError, block_number, parse_quantity, validate_lock, verify_reth_source

JSONRPC = "2.0"


class Fixture:
    def __init__(self, blocks: list[dict[str, Any]]) -> None:
        self.blocks = {block_number(block): block for block in blocks}
        self.first = min(self.blocks)
        self.last = max(self.blocks)
        self.requests: list[tuple[str, bool]] = []

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC, "id": request_id, "error": {"code": code, "message": message}}

    def response(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return self.error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if request.get("jsonrpc") != JSONRPC or "id" not in request:
            return self.error(request_id, -32600, "Invalid Request")
        if request.get("method") != "eth_getBlockByNumber":
            return self.error(request_id, -32601, "Method not found")
        params = request.get("params")
        if (
            not isinstance(params, list)
            or len(params) != 2
            or not isinstance(params[1], bool)
        ):
            return self.error(request_id, -32602, "Invalid params")
        selector = params[0]
        self.requests.append((selector, params[1]))
        if selector == "latest":
            number = self.last
        else:
            try:
                number = parse_quantity(selector)
            except CorpusError:
                return self.error(request_id, -32602, "Invalid params")
        block = self.blocks.get(number)
        if block is None:
            return self.error(
                request_id,
                -32001,
                f"Block outside locked range {self.first}..{self.last}",
            )
        result = block
        if not params[1]:
            result = dict(block)
            result["transactions"] = [
                transaction["hash"] if isinstance(transaction, dict) else transaction
                for transaction in block["transactions"]
            ]
        return {"jsonrpc": JSONRPC, "id": request_id, "result": result}


class Handler(BaseHTTPRequestHandler):
    fixture: Fixture
    server_version = "reth-cp1-fixture/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, response: dict[str, Any]) -> None:
        body = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json(405, Fixture.error(None, -32600, "POST required"))

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 1 or length > 1024 * 1024:
                raise ValueError
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._json(400, Fixture.error(None, -32700, "Parse error"))
            return
        self._json(200, self.fixture.response(request))


def make_server(fixture: Fixture, port: int = 0) -> ThreadingHTTPServer:
    handler = type("LockedFixtureHandler", (Handler,), {"fixture": fixture})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8547)
    parser.add_argument(
        "--reth-source",
        type=Path,
        default=os.environ.get("RETH_V2_SOURCE"),
        required="RETH_V2_SOURCE" not in os.environ,
    )
    args = parser.parse_args()
    try:
        verify_reth_source(args.reth_source)
        lock, blocks = validate_lock(args.contract, args.corpus, args.lock)
    except CorpusError as error:
        parser.error(str(error))
    server = make_server(Fixture(blocks), args.port)
    print(
        f"serving {lock['canonical_identity']} read-only at "
        f"http://127.0.0.1:{server.server_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
