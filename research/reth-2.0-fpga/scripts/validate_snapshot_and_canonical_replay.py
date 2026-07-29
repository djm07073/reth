#!/usr/bin/env python3
"""Guardrail grader for snapshot identity and canonical replay."""

from baseline_contract import INPUT_PREFLIGHT, ValidationError, validate_input_preflight


def main() -> int:
    try:
        if INPUT_PREFLIGHT.is_file():
            report = validate_input_preflight()
            print(
                "SKIP valid BENCHMARK_INPUT_UNAVAILABLE: "
                f"snapshot archive/inventory absent; evidence_sha256={report['evidence_sha256']}"
            )
            return 0
        raise ValidationError("snapshot replay evidence is absent")
    except ValidationError as error:
        print(f"FAIL {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
