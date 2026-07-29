# CP-1 snapshot restore and short correctness replay

This harness restores one declared immutable Reth v2.0.0 snapshot into a new
disposable datadir and runs exactly 8 (or another explicitly declared first
warm-up slice of 8 through 32) real `reth-bench new-payload-fcu` payloads. It is a
correctness gate only. It does not run the 100+500 benchmark or produce
performance evidence.

The run manifest binds the CP-0 contract, pinned source and compiler, separately
built `reth` and `reth-bench` binaries, the unchanged OH-37 corpus lock, the
snapshot archive and complete unpacked inventory, anchor, and replay slice.
All hashes and paths must be concrete. `replay-manifest.example.json` is schema
documentation and cannot pass validation as-is.

## Snapshot prerequisite

The external snapshot must be a zstd-compressed tar whose paths are relative to
the exact custom Reth datadir passed to `reth node --datadir` (normally
including `db/mdbx.dat` and `static_files` at the archive root). Its external
inventory uses `snapshot-inventory.example.json` as its schema and lists every
regular file with byte length and SHA-256. Archive, inventory, corpus, and
corpus lock should be mounted read-only. The harness hashes the archive before
and after restore, rejects unsafe archive paths and symlinks, verifies the
complete restored inventory, and never starts Reth until all declared
provenance checks pass.

Do not use production/mainnet node state. If the declared immutable snapshot is
absent or requires paid/private infrastructure, stop rather than constructing
or substituting another state.

## Build and manifest

In the pinned checkout at
`eb4c15e5e36d8776d46629beae4c0a69af7ab04f`:

```bash
RUSTFLAGS='-C target-cpu=native' cargo +1.93.0 build --locked \
  --profile maxperf --package reth --bin reth
RUSTFLAGS='-C target-cpu=native' cargo +1.93.0 build --locked \
  --profile maxperf --package reth-bench --bin reth-bench
rustc +1.93.0 -vV | shasum -a 256
shasum -a 256 target/maxperf/reth target/maxperf/reth-bench
```

Copy the example outside Git, fill every field, and hash the exact checked-in
CP-0 contract. The manifest and reports contain paths and hashes, never JWT
contents, credentials, or endpoints.

`validate` hashes and checks the snapshot and its complete inventory as well as
the contract, source, toolchain, exact CP-0 build commands, binaries, and corpus.
It may therefore take time proportional to the snapshot size, but it never
extracts or starts a process.

## Validation and two independent restores

```bash
PYTHONWARNINGS=error python3 \
  research/reth-2.0-fpga/cp1-replay/correctness.py validate \
  --manifest "$RETH_CP1_REPLAY_MANIFEST"

PYTHONWARNINGS=error python3 \
  research/reth-2.0-fpga/cp1-replay/correctness.py run \
  --manifest "$RETH_CP1_REPLAY_MANIFEST" \
  --report artifacts/cp1-correctness/restore-1.json \
  --work-root "$RETH_CP1_WORK_ROOT"

PYTHONWARNINGS=error python3 \
  research/reth-2.0-fpga/cp1-replay/correctness.py run \
  --manifest "$RETH_CP1_REPLAY_MANIFEST" \
  --report artifacts/cp1-correctness/restore-2.json \
  --work-root "$RETH_CP1_WORK_ROOT"

PYTHONWARNINGS=error python3 \
  research/reth-2.0-fpga/cp1-replay/correctness.py compare \
  artifacts/cp1-correctness/restore-1.json \
  artifacts/cp1-correctness/restore-2.json
```

Each run uses a fresh randomly named directory and removes it after bounded
process shutdown. It preserves at most the final 1 MiB of each process log next
to the report in `<report-stem>.logs/`; `--keep-workdir` is available only for
diagnosis. The Engine proxy forwards the benchmark's authenticated requests
unchanged and records every `newPayload` and FCU status. After replay, the
harness queries Reth's canonical RPC and compares each output block hash and
state root with the locked corpus. The stable digest excludes runtime paths,
ports, timings, and logs, so reports from independent restores must have
identical correctness payloads byte-for-byte.

Run deterministic tests and unchanged predecessor validators with:

```bash
PYTHONWARNINGS=error python3 -m unittest discover \
  -s research/reth-2.0-fpga/cp1-replay/tests -v
PYTHONWARNINGS=error python3 -m unittest discover \
  -s research/reth-2.0-fpga/cp1-corpus/tests -v
PYTHONWARNINGS=error python3 \
  research/reth-2.0-fpga/experiment-contract/v1/validate.py
```
