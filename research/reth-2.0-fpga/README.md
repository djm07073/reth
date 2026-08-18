# Reth 2.0 persistence profiling kit

This directory contains the macOS harness used to isolate Reth persistence work from execution,
render Samply CPU/off-CPU flame graphs, and reduce the added MDBX/trie metrics into an FPGA target
report. The frozen 500-block corpus, accepted snapshot, binaries, and generated profiles are not
stored in git.

The exact retained experiment used Reth `v2.0.0` source commit
`eb4c15e5e36d8776d46629beae4c0a69af7ab04f`; this branch is based on that exact commit.
`reth-bench` has since been removed from the current main workspace, so replay uses the retained
benchmark binary (or a build from the frozen commit), not a newly downloaded data set.

The custom C implementation was developed from libMDBX v0.13.7 commit
`566b0f93c7c9a3bdffb8fb3dc0ce8ca42641bd72`. Its modular-source commit is preserved as
[`libmdbx-patches/0001-perf-cursor-add-atomic-duplicate-replacement-batches.patch.gz`](libmdbx-patches/0001-perf-cursor-add-atomic-duplicate-replacement-batches.patch.gz);
the generated amalgamation is already applied under `crates/storage/libmdbx-rs/mdbx-sys/libmdbx`.

## What is measured

- deterministic 20-block warm-up followed by a 100-block discovery slice or all 500 measured
  blocks;
- forced-persistence and standard Engine API request paths, including configurable block interval
  and persistence threshold;
- injection and post-injection drain wall time, process CPU, scheduler state, device I/O, and
  per-second persistence backlog/backpressure;
- `save_blocks` trie merge, account-trie write, storage-trie write, hashed-state write, and commit
  latency;
- MDBX cursor seek/delete/upsert call count, duration, logical/result bytes, and serialization;
- custom MDBX page-batch attempts, applied rate, replacements, pages, bytes, and fallback reasons;
- write-transaction dirty/retired bytes and MDBX newly/COW/clone/split/merge/spill/unspill/write/
  sync/prefault/mincore counters;
- Samply CPU and context-switch/off-CPU stacks, with standalone SVG flame graphs.

The operation timers intentionally time every selected cursor call and therefore add overhead.
Use the uninstrumented three-trial result as the performance baseline; use the instrumented run to
attribute time within persistence.

## Frozen artifact layout

Pass `--base` to the preflight script with this layout:

```text
<base>/
  source/reth-fixed/                 # commit eb4c15e...
  build/fixed/maxperf/reth
  build/fixed/maxperf/reth-bench
  corpus/corpus.jsonl
  corpus/corpus-lock.json
  compatibility-data/db/mdbx.dat
  cpu-baseline/summary.json
  cpu-baseline/trial-{1,2,3}/manifest.json
  evidence/preflight/stage-checkpoints.txt
  logs/grader-snapshot-canonical.log
```

Keep the output directory and snapshot on the same APFS volume. The runner uses copy-on-write
`cp -cR`, refuses to overwrite an existing run or datadir, binds only loopback ports, and fails
closed when hashes or the persisted endpoint do not match.

## Build the profiled node

For symbolized Samply stacks, build the instrumented node from this branch with:

```console
cargo build --profile maxperf-symbols --bin reth
shasum -a 256 target/maxperf-symbols/reth
```

Supply that hash through `--expected-reth-sha` and the branch commit through
`--expected-source-commit`. To reproduce the retained detailed run exactly, apply the profiling
patch at `eb4c15e...`; its node binary SHA-256 was
`962359985d444d022c75e77376d8428158cf2f5c8dcd1119741201303fdd8894`.

## Preflight and run

First verify the exact frozen inputs and Samply. Missing Xcode Instruments is recorded but does not
block the run.

```console
python3 research/reth-2.0-fpga/scripts/validate_oh165_preflight.py \
  --base "$FROZEN_BASE" \
  --goal-prompt "$GOAL_PROMPT" \
  --samply "$(command -v samply)" \
  --output "$RUN_ROOT/preflight.json"
```

Run the deterministic 100-block production-path discovery slice at a 500 ms input interval:

```console
python3 research/reth-2.0-fpga/scripts/profile_oh165.py \
  --run-id production-500ms-100 \
  --measured-blocks 100 \
  --measurement-request-mode production-standard \
  --block-interval-ms 500 \
  --persistence-threshold 2 \
  --profiler samply \
  --snapshot-datadir "$FROZEN_BASE/compatibility-data" \
  --datadir "$RUN_ROOT/datadirs/production-500ms-100" \
  --source "$RETH_SOURCE" \
  --expected-source-commit "$RETH_SOURCE_COMMIT" \
  --reth "$PROFILED_RETH" \
  --expected-reth-sha "$PROFILED_RETH_SHA256" \
  --reth-bench "$FROZEN_BASE/build/fixed/maxperf/reth-bench" \
  --samply "$(command -v samply)" \
  --corpus "$FROZEN_BASE/corpus/corpus.jsonl" \
  --corpus-lock "$FROZEN_BASE/corpus/corpus-lock.json" \
  --output "$RUN_ROOT/runs/production-500ms-100"
```

Use `--measured-blocks 500` for the full confirmation. Use
`--measurement-request-mode forced-persistence` to isolate the synchronous persistence wait. A
larger `--persistence-threshold` changes batching; the runner derives the corresponding
backpressure threshold and drains the final pending batch before validating the cold reopen.

If the immutable corpus ends at the measured endpoint, a larger batch may need future trigger
blocks that the lock correctly refuses to serve. Do not extend the corpus implicitly. Preserve the
injection-end boundary and use `recover_locked_corpus_tail.py` to replay only the pending measured
tail with an exact temporary threshold, followed by a cold reopen with the original threshold. The
recovery manifest is durability evidence; it does not manufacture a native original-threshold
drain-latency sample.

## Reduce profiles and metrics

```console
python3 research/reth-2.0-fpga/scripts/analyze_samply.py \
  --profile "$RUN/samply-profile.json.gz" \
  --manifest "$RUN/manifest.json" \
  --output "$RUN/samply-analysis.json"

python3 research/reth-2.0-fpga/scripts/render_samply_flamegraph.py \
  --profile "$RUN/samply-profile.json.gz" \
  --manifest "$RUN/manifest.json" \
  --mode cpu \
  --output "$RUN/flamegraph-cpu.svg"

python3 research/reth-2.0-fpga/scripts/render_samply_flamegraph.py \
  --profile "$RUN/samply-profile.json.gz" \
  --manifest "$RUN/manifest.json" \
  --mode offcpu \
  --output "$RUN/flamegraph-offcpu.svg"

python3 research/reth-2.0-fpga/scripts/reduce_fpga_detail.py \
  --before "$RUN/metrics-before.prom" \
  --after "$RUN/metrics-injection-end.prom" \
  --manifest "$RUN/manifest.json" \
  --samply-analysis "$RUN/samply-analysis.json" \
  --output "$RUN/fpga-detail.json"
```

The injection-end scrape is the deterministic measured-block window used in the benchmark report.
Pass `metrics-after.prom` instead when the reducer should also include the final persistence drain.

`finalize_oh165.py` is the strict reducer for the retained discovery/full pair and checks the
specific causal stack identifier found in that experiment. `reduce_oh155_baseline.py` re-derives
the three-trial unprofiled baseline. `fixture_oh155.py` serves the immutable corpus over loopback so
the benchmark never depends on an external RPC endpoint.

The proposed accelerator boundary and correctness gates are documented in
[`docs/design/fpga-mdbx-page-mutation-accelerator.md`](../../docs/design/fpga-mdbx-page-mutation-accelerator.md).
The implemented libMDBX CPU batch control, its failed discovery performance gate, and the required
next final-page-builder design are documented in
[`docs/design/cpu-page-batch-reference-engine.md`](../../docs/design/cpu-page-batch-reference-engine.md)
and
[`docs/design/cpu-page-batch-benchmark-report.md`](../../docs/design/cpu-page-batch-benchmark-report.md).
The accepted software COW candidate, fair batch-17/batch-33 comparison, and full 500-block
confirmation are documented in
[`docs/design/cow-transaction-batching-benchmark-report.md`](../../docs/design/cow-transaction-batching-benchmark-report.md).
