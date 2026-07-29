#!/usr/bin/env python3
"""Primary grader for the SC-01 CPU-only baseline."""

from baseline_contract import INPUT_PREFLIGHT, ValidationError, validate_baseline, validate_input_preflight


def main() -> int:
    try:
        if INPUT_PREFLIGHT.is_file():
            report = validate_input_preflight()
            print(
                "BENCHMARK_INPUT_UNAVAILABLE evidence valid; "
                f"evidence_sha256={report['evidence_sha256']}; baseline_result=BLOCKED"
            )
            return 2
        summary = validate_baseline()
        print(
            "PASS valid_trials=6 "
            f"median_wait_for_persistence_e2e_wall_clock_seconds="
            f"{summary['median_wait_for_persistence_e2e_wall_clock_seconds']}"
        )
        return 0
    except ValidationError as error:
        print(f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
