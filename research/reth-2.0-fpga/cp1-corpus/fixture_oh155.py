#!/usr/bin/env python3
"""Serve the immutable OH-155 corpus from disk over loopback JSON-RPC."""

from __future__ import annotations

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Fixture:
    def __init__(self, corpus: Path, lock_path: Path) -> None:
        lock = json.loads(lock_path.read_text())
        if sha256(corpus) != lock["artifact"]["sha256"]:
            raise ValueError("corpus checksum mismatch")
        self.corpus = corpus
        self.offsets: dict[int, int] = {}
        with corpus.open("rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                block = json.loads(line)
                self.offsets[int(block["number"], 16)] = offset
        expected = lock["range"]
        if len(self.offsets) != expected["blocks"]:
            raise ValueError("corpus block count mismatch")
        if min(self.offsets) != expected["from"] or max(self.offsets) != expected["to"]:
            raise ValueError("corpus range mismatch")

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def response(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return self.error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if request.get("method") == "eth_chainId":
            return {"jsonrpc": "2.0", "id": request_id, "result": "0x1"}
        if request.get("method") == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": request_id, "result": hex(max(self.offsets))}
        if request.get("method") != "eth_getBlockByNumber":
            return self.error(request_id, -32601, "Method not found")
        params = request.get("params")
        if not isinstance(params, list) or len(params) != 2 or not isinstance(params[1], bool):
            return self.error(request_id, -32602, "Invalid params")
        try:
            number = max(self.offsets) if params[0] == "latest" else int(params[0], 16)
        except (TypeError, ValueError):
            return self.error(request_id, -32602, "Invalid params")
        offset = self.offsets.get(number)
        if offset is None:
            return self.error(request_id, -32001, "Block outside locked range")
        with self.corpus.open("rb") as stream:
            stream.seek(offset)
            block = json.loads(stream.readline())
        if not params[1]:
            block["transactions"] = [tx["hash"] if isinstance(tx, dict) else tx for tx in block["transactions"]]
        return {"jsonrpc": "2.0", "id": request_id, "result": block}


class Handler(BaseHTTPRequestHandler):
    fixture: Fixture

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            body = json.dumps(self.fixture.response(request), separators=(",", ":")).encode()
            self.send_response(200)
        except (ValueError, json.JSONDecodeError):
            body = json.dumps(Fixture.error(None, -32700, "Parse error")).encode()
            self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8547)
    args = parser.parse_args()
    fixture = Fixture(args.corpus, args.lock)
    handler = type("OH155Handler", (Handler,), {"fixture": fixture})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"serving {len(fixture.offsets)} locked blocks on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
