from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from baseline_contract import (  # noqa: E402
    BLOCKER_ID,
    CORPUS_LOCK_SHA256,
    CORPUS_SHA256,
    ORDERED_HASH_SHA256,
    RETH_COMMIT,
    SNAPSHOT_HEAD,
    SNAPSHOT_IDENTITY,
    ValidationError,
    evidence_digest,
    validate_input_preflight,
)


def valid_report() -> dict:
    report = {
        "schema_version": "reth-fpga-input-preflight/v1",
        "blocker_id": BLOCKER_ID,
        "result": "BLOCKED",
        "expected": {
            "reth_commit": RETH_COMMIT,
            "corpus_sha256": CORPUS_SHA256,
            "corpus_lock_sha256": CORPUS_LOCK_SHA256,
            "ordered_block_hashes_sha256": ORDERED_HASH_SHA256,
            "snapshot_logical_identity": SNAPSHOT_IDENTITY,
            "snapshot_head": SNAPSHOT_HEAD,
        },
        "observed": {
            "source": {"commit": RETH_COMMIT},
            "corpus": {
                "present": True,
                "sha256": CORPUS_SHA256,
                "lock_sha256": CORPUS_LOCK_SHA256,
                "blocks": 664,
            },
            "snapshot": {"present": False},
            "official_snapshot_source": {
                "url": "https://snapshots.reth.rs/",
                "http_status": 200,
                "offered_blocks": [25_632_375],
                "exact_snapshot_head_listed": False,
            },
        },
        "attempted_authorized_local_sources": ["/snapshot", "https://snapshots.reth.rs/"],
        "missing_required_material": [
            "snapshot_archive_and_inventory",
            "permanent_corpus_artifact",
        ],
        "authority": {"aws_or_paid_resources_used": False, "credentials_used": False},
        "execution_decision": "do_not_launch_primary_trials",
    }
    report["evidence_sha256"] = evidence_digest(report)
    return report


class InputPreflightTests(unittest.TestCase):
    def write(self, report: dict) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "preflight.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_valid_blocker(self) -> None:
        report = valid_report()
        self.assertEqual(validate_input_preflight(self.write(report)), report)

    def test_digest_tamper_fails(self) -> None:
        report = valid_report()
        report["execution_decision"] = "launch_primary_trials"
        with self.assertRaisesRegex(ValidationError, "digest"):
            validate_input_preflight(self.write(report))

    def test_exact_snapshot_listing_fails(self) -> None:
        report = valid_report()
        report["observed"]["official_snapshot_source"]["offered_blocks"] = [SNAPSHOT_HEAD]
        report["evidence_sha256"] = evidence_digest(report)
        with self.assertRaisesRegex(ValidationError, "unexpectedly offered"):
            validate_input_preflight(self.write(report))

    def test_paid_resource_use_fails(self) -> None:
        report = valid_report()
        report["authority"]["aws_or_paid_resources_used"] = True
        report["evidence_sha256"] = evidence_digest(report)
        with self.assertRaisesRegex(ValidationError, "paid resource"):
            validate_input_preflight(self.write(report))


if __name__ == "__main__":
    unittest.main()
