#!/usr/bin/env python3
"""Guardrail grader for the primary instrumentation overhead."""

from baseline_contract import (
    BASELINE_ROOT,
    INPUT_PREFLIGHT,
    ValidationError,
    load_json,
    require,
    validate_input_preflight,
)


def main() -> int:
    try:
        if INPUT_PREFLIGHT.is_file():
            report = validate_input_preflight()
            print(
                "SKIP valid BENCHMARK_INPUT_UNAVAILABLE: no primary trial launched; "
                f"evidence_sha256={report['evidence_sha256']}"
            )
            return 0
        overhead = load_json(BASELINE_ROOT / "profiling_overhead.json")
        require(overhead.get("schema_version") == "reth-fpga-profiling-overhead/v1", "schema")
        require(overhead.get("primary_overhead_pct", 101) <= 3, "primary overhead exceeds 3%")
        print(f"PASS primary_overhead_pct={overhead['primary_overhead_pct']}")
        return 0
    except ValidationError as error:
        print(f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
