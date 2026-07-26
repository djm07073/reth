# CP-1 locked offline block corpus

This directory materializes and serves the physical input boundary frozen by
`../experiment-contract/v1/manifest.json`. It covers Ethereum mainnet blocks
20,999,936 through 21,000,599 (664 blocks), including the `N-32` and `N-64`
headers requested by the pinned `reth-bench new-payload-fcu` client. It does not
restore a Reth database or run the 100+500 block benchmark.

The full-transaction JSONL corpus and generated `corpus-lock.json` stay outside
Git. The lock binds the corpus bytes, ordered block hashes, fixture server,
range, chain, pinned commits, and frozen boundary hashes/roots. The checked-in
`corpus-lock.example.json` is schema documentation only.

## Prerequisites

Use a separate checkout whose `HEAD` is exactly
`eb4c15e5e36d8776d46629beae4c0a69af7ab04f`. Every generate, validate, serve,
and preflight entry point fails closed if `RETH_V2_SOURCE` points elsewhere.

```bash
export RETH_V2_SOURCE=/absolute/path/to/reth-v2.0.0
export RETH_CP1_CORPUS_PATH=/absolute/path/outside/repository/corpus.jsonl
export RETH_CP1_CORPUS_LOCK=/absolute/path/outside/repository/corpus-lock.json
```

## One-time generation

The upstream must be a trusted mainnet archive endpoint. It is used only by the
materializer and is never recorded in the lock. Do not put credentials in shell
history, Git, logs, or Linear; inject a credential-bearing URL through a
protected environment when needed.

```bash
python3 research/reth-2.0-fpga/cp1-corpus/materialize.py \
  --contract research/reth-2.0-fpga/experiment-contract/v1/manifest.json \
  --corpus "$RETH_CP1_CORPUS_PATH" \
  --lock "$RETH_CP1_CORPUS_LOCK" \
  --upstream "$RETH_CP1_UPSTREAM_RPC"
```

## Validate and serve offline

Validation rereads every byte and block. The server binds only to loopback,
contains no upstream URL or network fallback, maps `latest` to block 21,000,599,
and accepts only canonical numeric quantities within the locked range.

```bash
python3 research/reth-2.0-fpga/experiment-contract/v1/validate.py

python3 research/reth-2.0-fpga/cp1-corpus/validate.py \
  --contract research/reth-2.0-fpga/experiment-contract/v1/manifest.json \
  --corpus "$RETH_CP1_CORPUS_PATH" \
  --lock "$RETH_CP1_CORPUS_LOCK"

python3 research/reth-2.0-fpga/cp1-corpus/fixture_server.py \
  --contract research/reth-2.0-fpga/experiment-contract/v1/manifest.json \
  --corpus "$RETH_CP1_CORPUS_PATH" \
  --lock "$RETH_CP1_CORPUS_LOCK" \
  --port 8547
```

## Short offline request-capture preflight

Build the pinned client separately, then verify its command surface and pinned
source request semantics. The preflight starts the validated fixture on an
ephemeral loopback port, runs the actual `new-payload-fcu` binary, and captures
its numeric `N-32`/`N-64` requests. A loopback Engine stub then returns an
intentional error so the command cannot execute a payload. Separate direct
requests verify `latest` and the declared boundaries, then a fresh fixture
instance must reproduce the same response digest.

```bash
(cd "$RETH_V2_SOURCE" && \
  RUSTFLAGS='-C target-cpu=native' cargo +1.93.0 build --locked \
    --profile maxperf --package reth-bench --bin reth-bench)

python3 research/reth-2.0-fpga/cp1-corpus/offline_preflight.py \
  --reth-bench "$RETH_V2_SOURCE/target/maxperf/reth-bench" \
  --corpus "$RETH_CP1_CORPUS_PATH" \
  --lock "$RETH_CP1_CORPUS_LOCK"
```

Run deterministic local tests with:

```bash
python3 -m unittest discover \
  -s research/reth-2.0-fpga/cp1-corpus/tests -v
```

No corpus payload, snapshot, JWT, endpoint, credential, or machine-specific
path belongs in this directory.
