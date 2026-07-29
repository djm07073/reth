# OH-96 deterministic input preflight

`input-preflight.json` is the machine-readable result of the bounded local,
free, no-credential input recovery attempt. The fixed 664-block corpus was
recovered and validated, but the required Reth v2.0-compatible snapshot archive
and inventory at exact head `20999999` were absent locally and were not listed
by the official snapshot index.

The corpus body is too large for an ordinary Git blob. The local recovered copy
matches the frozen SHA-256, but an immutable release asset could not be created:
the local GitHub CLI credential is invalid and the connected GitHub contents
API has no binary release-asset operation. The lock, official-index response,
preflight, and graders are preserved on the canonical OH-96 branch; the
preflight therefore also names `permanent_corpus_artifact` as unavailable.

Verify after download:

```bash
shasum -a 256 /path/to/corpus.jsonl
# d7e91e27d946659e678da4d970c9c059d75bae11ea9addec22cbd043b56a7c96

RETH_FPGA_CORPUS=/path/to/corpus.jsonl \
RETH_FPGA_CORPUS_LOCK="$PWD/artifacts/oh-96/inputs/corpus-lock.json" \
RETH_V2_SOURCE=/path/to/reth-at-eb4c15e5e36d8776d46629beae4c0a69af7ab04f \
python3 research/reth-2.0-fpga/cp1-corpus/scripts/validate_locked_corpus.py
```

The primary grader exits `2` with `baseline_result=BLOCKED`. No latency,
throughput, profiling-overhead, or six-trial result exists because preflight
prevented an invalid launch.
