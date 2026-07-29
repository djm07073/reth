# SC-01 CPU baseline graders

The scripts in this directory fail closed against the frozen SC-01 contract. Run
`input_preflight.py` before acquiring or restoring any snapshot. A valid
`BENCHMARK_INPUT_UNAVAILABLE` report prevents primary trials when the exact
head-20999999 archive and inventory are absent; it is not a performance result.

The primary grader accepts either exactly six complete, comparable trial
manifests or the predeclared blocker report. In the blocker case it exits `2`,
prints `baseline_result=BLOCKED`, and never fabricates latency or throughput.
