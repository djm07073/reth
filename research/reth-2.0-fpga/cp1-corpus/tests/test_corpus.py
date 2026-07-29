from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

CP1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP1))

from corpus import (  # noqa: E402
    CorpusError,
    build_lock,
    canonical_json,
    contract_values,
    read_corpus,
    validate_lock,
    verify_reth_source,
    write_canonical,
)
from fixture_server import Fixture, make_server  # noqa: E402
from offline_preflight import capture_fixture  # noqa: E402

CONTRACT = CP1.parent / "experiment-contract/v1/manifest.json"
START = 20_999_936
END = 21_000_599


def hash_for(number: int, salt: int = 0) -> str:
    return "0x" + f"{number + salt:064x}"


def synthetic_blocks() -> list[dict]:
    blocks = []
    for number in range(START, END + 1):
        blocks.append(
            {
                "number": hex(number),
                "hash": hash_for(number),
                "parentHash": hash_for(number - 1),
                "stateRoot": hash_for(number, 1_000_000),
                "transactions": [{"hash": hash_for(number, 2_000_000)}],
            }
        )
    return blocks


def synthetic_contract(blocks: list[dict]) -> dict:
    contract = json.loads(CONTRACT.read_text())
    by_number = {int(block["number"], 16): block for block in blocks}
    workload = contract["workload"]
    workload["snapshot_head"]["hash"] = by_number[20_999_999]["hash"]
    workload["snapshot_head"]["state_root"] = by_number[20_999_999]["stateRoot"]
    workload["warmup"]["first_block_hash"] = by_number[21_000_000]["hash"]
    workload["warmup"]["last_block_hash"] = by_number[21_000_099]["hash"]
    workload["measurement"]["first_block_hash"] = by_number[21_000_100]["hash"]
    workload["measurement"]["last_block_hash"] = by_number[21_000_599]["hash"]
    workload["measurement"]["last_state_root"] = by_number[21_000_599]["stateRoot"]
    return contract


class CorpusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blocks = synthetic_blocks()
        self.contract = synthetic_contract(self.blocks)
        self.contract_path = self.root / "manifest.json"
        self.corpus_path = self.root / "corpus.jsonl"
        self.lock_path = self.root / "corpus-lock.json"
        write_canonical(self.contract_path, self.contract)
        self.corpus_path.write_bytes(
            b"".join(canonical_json(block) + b"\n" for block in self.blocks)
        )
        self.values = contract_values(self.contract)
        write_canonical(
            self.lock_path, build_lock(self.corpus_path, self.blocks, self.values)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_range_and_lock_are_deterministic(self) -> None:
        first, blocks = validate_lock(
            self.contract_path, self.corpus_path, self.lock_path
        )
        second = build_lock(self.corpus_path, read_corpus(self.corpus_path), self.values)
        self.assertEqual(first, second)
        self.assertEqual(len(blocks), 664)
        self.assertEqual(first["range"], {"from": START, "to": END, "blocks": 664})

    def test_independent_offline_runs_have_identical_responses(self) -> None:
        lock, blocks = validate_lock(
            self.contract_path, self.corpus_path, self.lock_path
        )
        first = capture_fixture(blocks, lock)
        second = capture_fixture(blocks, lock)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 13)

    def test_one_byte_mutation_is_rejected(self) -> None:
        data = bytearray(self.corpus_path.read_bytes())
        data[data.index(b'"stateRoot"')] = ord("S")
        self.corpus_path.write_bytes(data)
        with self.assertRaises(CorpusError):
            validate_lock(self.contract_path, self.corpus_path, self.lock_path)

    def test_missing_and_reordered_blocks_are_rejected(self) -> None:
        for changed in (self.blocks[:-1], [self.blocks[1], self.blocks[0], *self.blocks[2:]]):
            with self.subTest(length=len(changed)):
                self.corpus_path.write_bytes(
                    b"".join(canonical_json(block) + b"\n" for block in changed)
                )
                with self.assertRaises(CorpusError):
                    build_lock(
                        self.corpus_path, read_corpus(self.corpus_path), self.values
                    )

    def test_frozen_boundary_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(self.blocks)
        changed[63]["hash"] = hash_for(1)
        changed[64]["parentHash"] = changed[63]["hash"]
        self.corpus_path.write_bytes(
            b"".join(canonical_json(block) + b"\n" for block in changed)
        )
        with self.assertRaisesRegex(CorpusError, "frozen hash mismatch"):
            build_lock(self.corpus_path, changed, self.values)

    def test_mismatched_reth_checkout_fails_closed(self) -> None:
        repository = self.root / "reth"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "CP1 test"], check=True
        )
        (repository / "README").write_text("test")
        subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "test"], check=True)
        with self.assertRaisesRegex(CorpusError, "checkout mismatch"):
            verify_reth_source(repository)


class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.blocks = synthetic_blocks()
        cls.server = make_server(Fixture(cls.blocks))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method: str, params: list, request_id: int = 1) -> dict:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_declared_boundaries_and_offline_second_run_match(self) -> None:
        numbers = (20_999_936, 20_999_999, 21_000_000, 21_000_099, 21_000_100, 21_000_599)
        first = [
            self.request("eth_getBlockByNumber", [hex(number), True], number)["result"]
            for number in numbers
        ]
        second = [
            self.request("eth_getBlockByNumber", [hex(number), True], number)["result"]
            for number in numbers
        ]
        self.assertEqual(first, second)
        self.assertEqual([int(block["number"], 16) for block in first], list(numbers))

    def test_latest_and_safe_finalized_numeric_lookbacks(self) -> None:
        latest = self.request("eth_getBlockByNumber", ["latest", True])["result"]
        self.assertEqual(int(latest["number"], 16), END)
        for distance in (32, 64):
            result = self.request(
                "eth_getBlockByNumber", [hex(21_000_000 - distance), False]
            )["result"]
            self.assertEqual(int(result["number"], 16), 21_000_000 - distance)
            self.assertIsInstance(result["transactions"][0], str)

    def test_unsupported_method_range_and_noncanonical_quantity(self) -> None:
        method = self.request("eth_chainId", [])
        outside = self.request("eth_getBlockByNumber", [hex(START - 1), False])
        quantity = self.request("eth_getBlockByNumber", ["0x01406f00", False])
        self.assertEqual(method["error"]["code"], -32601)
        self.assertEqual(outside["error"]["code"], -32001)
        self.assertEqual(quantity["error"]["code"], -32602)

    def test_get_is_read_only_error(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.url)
        with raised.exception:
            self.assertEqual(raised.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
