# CP-0 experiment contract decision

Status: frozen

Contract: `reth-fpga-experiment-contract/v1`

Linear: `OH-35`

Goal criterion: `SC-01.1`

Reth and `reth-bench` are independently built from the same upstream v2.0.0 commit,
`eb4c15e5e36d8776d46629beae4c0a69af7ab04f`. The replay identity is canonical
Ethereum mainnet blocks 21,000,000 through 21,000,599 from a snapshot at
20,999,999. The first 100 blocks warm caches and the remaining 500 blocks form
the measured sample. The local replay fixture additionally includes canonical
headers back to 20,999,936 for the safe/finalized lookbacks. Because v2.0.0
treats `--from` as the existing anchor, the commands use 20,999,999 and
21,000,099; this preserves the declared 100/500 payload counts. A run is
inadmissible unless its corpus, snapshot, binaries, commands, and environment
are content-addressed in the run artifacts.

CPU and FPGA runs use the same `reth-bench` binary, corpus, restored snapshot,
cache state, host, boot, build profile, and completion condition. The measured
FPGA boundary includes runtime, PCIe/DMA, serialization, queueing,
synchronization, CPU orchestration, timeout, and fallback. Six paired,
interleaved comparisons are the minimum in every mode/cache-state cell; no
outlier is removed. Correctness is a gate, not a performance metric.
The frozen primary endpoint is warm `wait-for-persistence` E2E wall-clock:
median improvement must be at least 25% and its paired-bootstrap 95% confidence
interval must exclude zero. Every non-primary cell has a 3% median E2E
no-regression guardrail.

The main run admits only instrumentation whose paired overhead is at most 3%.
Higher-overhead observations move to a separately labelled instrumented run and
cannot provide the primary end-to-end result.

The referenced Reth PR evidence is frozen in `reth-evidence.csv`. It records a
post-v2.0 state-root counterfactual with more than 25% big-block headroom, two
post-v2.0 changes whose paired replay effects stayed below 2%, one cursor
microbenchmark later superseded by another post-v2.0 change, and one paired
replay result already present in v2.0.0. The state-root result deliberately
bypasses correctness and uses a synthetic workload; the other observations do
not show sufficient headroom. None selects an FPGA target without repetition on
the frozen corpus.

## Goal mapping

| Criterion | Contract contribution |
| --- | --- |
| SC-01 | Pins source, workload, environment equivalence, repetition, statistics, artifacts, and correctness before implementation. |
| SC-02 | Freezes the application/kernel/MDBX/Trie inventory and the 3% overhead gate for full-stack observation. |
| SC-03 | Requires six paired runs, cold/warm cache states, persistence completion, and raw DB/Trie dependency, scan, batchability, and projected PCIe-transfer traces. |
| SC-05 | Prevents target selection before causal headroom and preserves identical CPU/FPGA comparison boundaries. |

No AWS F1 instance may be launched under this contract. A separate explicit
human approval is required before any paid resource is created.
