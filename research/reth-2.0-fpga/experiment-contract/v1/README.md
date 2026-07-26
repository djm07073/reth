# Reth 2.0 FPGA experiment contract v1

This directory is the frozen CP-0 measurement contract.

- `manifest.json` is the machine-readable experiment and comparison contract.
- `metrics.csv` is the required metric coverage matrix.
- `reth-evidence.csv` maps the five referenced Reth performance PRs to hypotheses
  and required measurements without treating microbenchmarks or profiles as E2E proof.
- `DECISION.md` records the checkpoint decision and root-goal mapping.
- `validate.py` rejects incomplete or internally inconsistent contracts.

Validate locally:

```sh
python3 research/reth-2.0-fpga/experiment-contract/v1/validate.py
```

Expected result:

```text
OK reth-2.0-fpga-cp0-v1: 73 metrics, 5 PRs, 6 pairs/cell, 100 warmup + 500 measured blocks
```

The manifest distinguishes the frozen logical identity of a corpus/snapshot from
its physical artifact. CP-1 materializes those files. Every run must then bind
their SHA-256 and size in `corpus-lock.json` and `snapshot-lock.json`; network
fetching during a measured run is forbidden. A read-only loopback JSON-RPC
fixture serves blocks 20,999,936 through 21,000,599 so `reth-bench` can also
resolve the canonical 32/64-block safe/finalized lookbacks.

In Reth v2.0.0, `--from` is the already-canonical anchor rather than the first
payload sent. The frozen commands therefore use 20,999,999 for the 100-block
warm-up and 21,000,099 for the 500-block measurement. `--output` names a
directory, matching the v2.0.0 CLI.

This contract does not authorize launching AWS F1 or any other paid resource.
