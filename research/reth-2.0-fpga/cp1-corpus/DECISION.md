# CP-1 corpus decision

## Scope

This increment locks and validates the physical block-corpus prerequisite for
SC-01.2. It does not claim snapshot restoration, payload execution,
state-root replay reproducibility, instrumentation coverage, benchmark
performance, or FPGA suitability.

## Acceptance boundary

The result passes only when an external 664-block corpus validates against the
CP-0 boundary hashes and roots, a second offline validation produces the same
lock and responses, mutation/reordering tests fail closed, and the pinned
`reth-bench` request-capture preflight resolves numeric `N-32` and `N-64`
lookbacks without an upstream connection.

## Result

Passed on 2026-07-26.

- The pinned Reth source was checked at
  `eb4c15e5e36d8776d46629beae4c0a69af7ab04f`.
- The external corpus contains exactly 664 blocks from 20,999,936 through
  21,000,599 and is 148,831,288 bytes.
- Corpus artifact SHA-256:
  `d7e91e27d946659e678da4d970c9c059d75bae11ea9addec22cbd043b56a7c96`.
- Ordered block-hashes SHA-256:
  `0c67edbed2dd6c42a0f1cb684800fd1d6de213f8bf0011722f59758467989280`.
- Fixture-server SHA-256:
  `afb6b25c1865a2d6b031d9a367b35d50220b5199483a0abbd48d8be470dcae49`.
- External `corpus-lock.json` SHA-256:
  `d79717b912aeb0826c06448fad1811dae7a65f824349890279a6a22c9b8f9b68`.
- Two independent offline fixture runs produced the same 13-response digest:
  `542b0d649f84c151318a3e01cca219534751efe7c4abc7edcff6538f5655a9bb`.
  Ten unit and loopback integration tests also rejected one-byte mutation,
  missing and reordered blocks, a mismatched checkout, unsupported methods,
  out-of-range requests, and noncanonical quantities.
- The separately built pinned `reth-bench` binary requested block
  `0x1406f01` (N-64) and `0x1406f21` (N-32) from the loopback-only fixture,
  then reached the intentional `engine_newPayloadV3` stop. No live RPC
  upstream was available to the serving process.

The offline corpus identity and client-facing lookback prerequisite therefore
pass. Snapshot restore, payload/state-root replay, the 100+500 benchmark,
performance measurement, and FPGA target selection remain unproven and out of
scope.
