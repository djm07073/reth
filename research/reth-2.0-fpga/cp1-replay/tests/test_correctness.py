from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "correctness.py"
SPEC = importlib.util.spec_from_file_location("cp1_correctness", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
correctness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(correctness)


def canonical_block(number: int, parent_hash: str) -> dict:
    block_hash = f"0x{number:064x}"
    return {
        "number": hex(number),
        "hash": block_hash,
        "parentHash": parent_hash,
        "stateRoot": f"0x{number + 1000:064x}",
        "transactions": [],
    }


class ReplayContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor_number = 99
        self.anchor_hash = f"0x{self.anchor_number:064x}"
        self.anchor_root = f"0x{999:064x}"
        self.contract = {
            "workload": {
                "chain": "ethereum-mainnet",
                "chain_id": 1,
                "snapshot": {
                    "format": "reth-datadir-tar-zstd",
                    "logical_identity": "snapshot",
                },
                "snapshot_head": {
                    "number": self.anchor_number,
                    "hash": self.anchor_hash,
                    "state_root": self.anchor_root,
                },
                "warmup": {"from": 100, "to": 199},
            }
        }
        self.manifest = {
            "chain": {"id": 1, "name": "mainnet"},
            "snapshot": {
                "format": "reth-datadir-tar-zstd",
                "logical_identity": "snapshot",
            },
            "anchor": {
                "number": self.anchor_number,
                "hash": self.anchor_hash,
                "state_root": self.anchor_root,
            },
            "replay": {"from": 100, "to": 107, "payloads": 8},
            "timeouts_seconds": {
                "node_ready": 120,
                "replay": 600,
                "process_stop": 20,
            },
        }
        parent = self.anchor_hash
        self.blocks = []
        for number in range(100, 108):
            block = canonical_block(number, parent)
            self.blocks.append(block)
            parent = block["hash"]

    def test_accepts_contiguous_eight_payload_slice(self) -> None:
        selected = correctness.validate_replay_contract(
            self.manifest, self.contract, self.blocks
        )
        self.assertEqual([int(block["number"], 16) for block in selected], list(range(100, 108)))

    def test_rejects_wrong_anchor(self) -> None:
        self.manifest["anchor"]["state_root"] = f"0x{1:064x}"
        with self.assertRaisesRegex(correctness.ReplayError, "anchor"):
            correctness.validate_replay_contract(self.manifest, self.contract, self.blocks)

    def test_rejects_reordered_corpus(self) -> None:
        self.blocks[3], self.blocks[4] = self.blocks[4], self.blocks[3]
        with self.assertRaisesRegex(correctness.ReplayError, "order"):
            correctness.validate_replay_contract(self.manifest, self.contract, self.blocks)

    def test_rejects_missing_payload(self) -> None:
        self.blocks.pop(4)
        with self.assertRaisesRegex(correctness.ReplayError, "fully present"):
            correctness.validate_replay_contract(self.manifest, self.contract, self.blocks)

    def test_rejects_short_slice(self) -> None:
        self.manifest["replay"] = {"from": 100, "to": 106, "payloads": 7}
        with self.assertRaisesRegex(correctness.ReplayError, "8..32"):
            correctness.validate_replay_contract(self.manifest, self.contract, self.blocks)

    def test_rejects_non_mainnet_chain_name(self) -> None:
        self.manifest["chain"]["name"] = "sepolia"
        with self.assertRaisesRegex(correctness.ReplayError, "Ethereum mainnet"):
            correctness.validate_replay_contract(self.manifest, self.contract, self.blocks)

    def test_rejects_unbounded_timeout(self) -> None:
        self.manifest["timeouts_seconds"]["replay"] = 3600
        with self.assertRaisesRegex(correctness.ReplayError, "within 1..900"):
            correctness.validate_replay_contract(self.manifest, self.contract, self.blocks)


class RestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "db").mkdir(parents=True)
        (self.root / "static_files").mkdir()
        self.db = self.root / "db/mdbx.dat"
        self.db.write_bytes(b"database")
        self.static = self.root / "static_files/headers"
        self.static.write_bytes(b"static")
        self.inventory = {
            "files": [
                {
                    "path": "db/mdbx.dat",
                    "bytes": self.db.stat().st_size,
                    "sha256": correctness.sha256_file(self.db),
                },
                {
                    "path": "static_files/headers",
                    "bytes": self.static.stat().st_size,
                    "sha256": correctness.sha256_file(self.static),
                },
            ]
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validates_complete_restore(self) -> None:
        correctness.validate_restored_tree(
            self.root,
            self.inventory,
            ["db/mdbx.dat", "static_files"],
        )

    def test_rejects_incomplete_restore(self) -> None:
        self.static.unlink()
        with self.assertRaisesRegex(correctness.ReplayError, "inventory mismatch"):
            correctness.validate_restored_tree(
                self.root,
                self.inventory,
                ["db/mdbx.dat", "static_files"],
            )

    def test_rejects_corrupted_restore(self) -> None:
        self.db.write_bytes(b"corrupt!")
        with self.assertRaisesRegex(correctness.ReplayError, "file mismatch"):
            correctness.validate_restored_tree(
                self.root,
                self.inventory,
                ["db/mdbx.dat", "static_files"],
            )

    def test_rejects_extra_restore_file(self) -> None:
        (self.root / "unexpected").write_bytes(b"x")
        with self.assertRaisesRegex(correctness.ReplayError, "inventory mismatch"):
            correctness.validate_restored_tree(
                self.root,
                self.inventory,
                ["db/mdbx.dat", "static_files"],
            )


class ResultTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = f"0x{99:064x}"
        self.blocks = []
        self.captures = []
        self.observed = {}
        for number in range(100, 108):
            block = canonical_block(number, parent)
            self.blocks.append(block)
            parent = block["hash"]
            self.captures.extend(
                [
                    {
                        "kind": "new_payload",
                        "method": "engine_newPayloadV3",
                        "block_number": number,
                        "block_hash": block["hash"],
                        "status": "VALID",
                        "latest_valid_hash": block["hash"],
                    },
                    {
                        "kind": "forkchoice_updated",
                        "method": "engine_forkchoiceUpdatedV3",
                        "head_block_hash": block["hash"],
                        "status": "VALID",
                        "latest_valid_hash": block["hash"],
                    },
                ]
            )
            self.observed[hex(number)] = {
                "hash": block["hash"],
                "stateRoot": block["stateRoot"],
            }

    def fake_rpc(self, _url: str, _method: str, params: list, timeout: float = 5) -> dict:
        del timeout
        return self.observed[params[0]]

    def test_builds_ordered_correctness_records(self) -> None:
        with patch.object(correctness, "rpc", side_effect=self.fake_rpc):
            records = correctness.build_block_records(self.blocks, self.captures, "unused")
        self.assertEqual(len(records), 8)
        self.assertTrue(all(record["new_payload_status"] == "VALID" for record in records))

    def test_rejects_engine_status(self) -> None:
        self.captures[0]["status"] = "SYNCING"
        with patch.object(correctness, "rpc", side_effect=self.fake_rpc):
            with self.assertRaisesRegex(correctness.ReplayError, "unexpected Engine status"):
                correctness.build_block_records(self.blocks, self.captures, "unused")

    def test_rejects_duplicate_engine_retry(self) -> None:
        self.captures.insert(1, dict(self.captures[0]))
        with patch.object(correctness, "rpc", side_effect=self.fake_rpc):
            with self.assertRaisesRegex(correctness.ReplayError, "capture count mismatch"):
                correctness.build_block_records(self.blocks, self.captures, "unused")

    def test_rejects_engine_call_reordering(self) -> None:
        self.captures[0], self.captures[1] = self.captures[1], self.captures[0]
        with patch.object(correctness, "rpc", side_effect=self.fake_rpc):
            with self.assertRaisesRegex(correctness.ReplayError, "call order"):
                correctness.build_block_records(self.blocks, self.captures, "unused")

    def test_rejects_state_root_mismatch_with_first_block(self) -> None:
        self.observed[hex(103)]["stateRoot"] = f"0x{1:064x}"
        with patch.object(correctness, "rpc", side_effect=self.fake_rpc):
            with self.assertRaisesRegex(correctness.ReplayError, "block 103"):
                correctness.build_block_records(self.blocks, self.captures, "unused")

    def test_two_independent_reports_compare_byte_stably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = correctness.finalize_report(
                {
                    "schema_version": correctness.REPORT_SCHEMA,
                    "manifest_sha256": "a" * 64,
                    "snapshot_artifact_sha256": "b" * 64,
                    "corpus_artifact_sha256": "c" * 64,
                    "anchor": {"number": 99, "hash": self.blocks[0]["parentHash"]},
                    "replay": {"from": 100, "to": 107, "payloads": 8},
                    "blocks": [{"block_number": number} for number in range(100, 108)],
                    "result": "PASS",
                }
            )
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(report), encoding="utf-8")
            second.write_text(json.dumps(report, indent=2), encoding="utf-8")
            correctness.compare_reports(first, second)

    def test_rejects_different_independent_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "schema_version": correctness.REPORT_SCHEMA,
                "manifest_sha256": "a" * 64,
                "snapshot_artifact_sha256": "b" * 64,
                "corpus_artifact_sha256": "c" * 64,
                "anchor": {"number": 99},
                "replay": {"from": 100, "to": 107, "payloads": 8},
                "blocks": [{"block_number": number} for number in range(100, 108)],
                "result": "PASS",
            }
            first_report = correctness.finalize_report(dict(base))
            changed = dict(base)
            changed["blocks"] = [{"block_number": number} for number in range(100, 107)]
            second_report = correctness.finalize_report(changed)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(first_report), encoding="utf-8")
            second.write_text(json.dumps(second_report), encoding="utf-8")
            with self.assertRaisesRegex(correctness.ReplayError, "differ"):
                correctness.compare_reports(first, second)


class ManifestTests(unittest.TestCase):
    def test_rejects_manifest_schema_mismatch_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
            with self.assertRaisesRegex(correctness.ReplayError, "schema"):
                correctness.validate_manifest(path)

    def test_rejects_unpinned_source_commit_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = root / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "software": {
                            "reth_commit": correctness.EXPECTED_RETH_COMMIT,
                            "reth_bench_commit": correctness.EXPECTED_RETH_COMMIT,
                        }
                    }
                ),
                encoding="utf-8",
            )
            path = root / "manifest.json"
            manifest = {
                "schema_version": correctness.MANIFEST_SCHEMA,
                "contract": {
                    "path": str(contract),
                    "sha256": correctness.sha256_file(contract),
                },
                "source": {"path": str(root), "commit": "0" * 40},
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(correctness.ReplayError, "not pinned"):
                correctness.validate_manifest(path)

    def test_rejects_unsafe_inventory_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            inventory = {
                "schema_version": correctness.INVENTORY_SCHEMA,
                "logical_identity": "snapshot",
                "files": [{"path": "../escape", "bytes": 1, "sha256": "a" * 64}],
            }
            inventory_path.write_bytes(correctness.canonical_json(inventory) + b"\n")
            manifest = {
                "snapshot": {
                    "inventory_path": str(inventory_path),
                    "inventory_sha256": correctness.sha256_file(inventory_path),
                    "logical_identity": "snapshot",
                }
            }
            with self.assertRaisesRegex(correctness.ReplayError, "invalid snapshot inventory"):
                correctness.load_inventory(root / "manifest.json", manifest)

    def test_rejects_invalid_inventory_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            inventory = {
                "schema_version": correctness.INVENTORY_SCHEMA,
                "logical_identity": "snapshot",
                "files": [{"path": "db", "bytes": 1, "sha256": "not-a-hash"}],
            }
            inventory_path.write_bytes(correctness.canonical_json(inventory) + b"\n")
            manifest = {
                "snapshot": {
                    "inventory_path": str(inventory_path),
                    "inventory_sha256": correctness.sha256_file(inventory_path),
                    "logical_identity": "snapshot",
                }
            }
            with self.assertRaisesRegex(correctness.ReplayError, "lowercase SHA-256"):
                correctness.load_inventory(root / "manifest.json", manifest)


if __name__ == "__main__":
    unittest.main()
