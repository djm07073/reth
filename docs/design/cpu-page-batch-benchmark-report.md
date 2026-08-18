# Reth v2 MDBX page-batch discovery report

## Outcome

The custom MDBX batch API is functionally integrated and correct on the deterministic 100-block
slice, but the first fixed-size replacement design is not a supported performance candidate. It
made the run 2.45% slower than the current-row control and did not reduce unique MDBX COW pages.
The full 500-block confirmation was therefore not run.

## Implemented boundary

- libMDBX C API: `mdbx_cursor_mutate_batch`
- low-level Rust FFI wrapper: `reth_libmdbx::Cursor<RW>::mutate_batch`
- typed Reth API: `DbDupCursorRW::replace_duplicates_batch`
- Reth integrations: `write_hashed_state` and `write_storage_trie_updates_sorted`
- Prometheus evidence: attempts, applied batches, fallback reasons, replacements, pages, and bytes
- safe fallback: delete, insert, variable-size value, subpage, not-found, and boundary/order cases

The C implementation validates the entire batch before page mutation. A batch that cannot use the
fast path returns an unsupported outcome without partial changes, after which Reth executes the
legacy cursor operations.

## Correctness incident and fix

The first mainnet replay (`cpu-page-batch-100-v1`) failed at block 25,661,087 with an incorrect
state root. The cause was causal and reproducible: after copying the first clean inner leaf path,
the new nested-tree root was not written to the outer duplicate node before resolving the next
leaf. The next `MDBX_GET_BOTH` reloaded the old root and discarded the earlier leaf update.

The fix publishes the nested-tree descriptor after each leaf group. Both the C and typed Rust tests
now commit fixture creation before mutation so they exercise clean COW paths. The corrected replay
processed setup 80, warm-up 20, measured 100, persistence drain, and cold reopen with matching final
block hash and state root.

## Measured evidence

| Metric | Clean baseline | Current-row control | Custom batch |
| --- | ---: | ---: | ---: |
| end-to-end wall clock | 706.640 s | 703.546 s | 720.820 s |
| injection wall clock | 683.970 s | 675.594 s | 697.840 s |
| process CPU | 525.32 core-s | 537.39 core-s | 520.89 core-s |
| persistence wait | 663.653 s | 656.648 s | 670.361 s |
| save-block MDBX | 612.023 s | 620.208 s | 615.596 s |
| hashed-state write | 160.014 s | 167.215 s | 155.609 s |
| trie update write | 449.458 s | 449.997 s | 457.607 s |
| commit MDBX | 72.021 s | 62.326 s | 75.809 s |

Against the current-row control, unique transaction COW pages changed from 986,524 to 986,538 and
split pages remained exactly 529. Internal page-touch/COW events rose from 828,827 to 1,242,400.
This is the decisive independent evidence: the API aggregated logical operations but did not
aggregate the physical page work that matters.

### Batch coverage

| Table | Attempts | Applied | Applied rate | Replacements | Source pages |
| --- | ---: | ---: | ---: | ---: | ---: |
| HashedStorages | 8,533 | 5,331 | 62.48% | 15,513 | 13,915 |
| StoragesTrie | 8,180 | 1,813 | 22.16% | 7,960 | 5,520 |

`StoragesTrie` fell back 4,227 times for boundary/order safety, 1,314 for variable encoded size,
698 for inline subpages, and 128 for not-found validation. `HashedStorages` fell back 1,222 times
for boundary/order safety, 1,091 for subpages, 831 for encoded size, and 58 for not-found.

## CPU/off-CPU and I/O interpretation

The batch run used fewer CPU core-seconds but more wall time. It spent 670.361 seconds waiting for
persistence (93.0% of end-to-end time), averaged 98.86 MiB/s and 7,836 IOPS on the sampled device,
and averaged 1.252 seconds per MDBX commit sync. Spills were zero. These measurements do not support
a CPU/FPGA target in the fixed-size replacement loop; the prototype increased page-touch work and
left durability/I/O waiting dominant.

Existing Samply CPU/off-CPU flame graphs remain part of the profiling kit's baseline evidence. This
candidate run used `--profiler none` so that its wall-clock comparison was not perturbed by a stack
profiler; it relies independently on per-block Prometheus, MDBX transaction/page counters, resource
sampling, and `iostat`.

## Next gate

Implement a CPU final-page builder that handles delete, insert, replace, and variable-size values
in one ordered mutation stream. It should resolve a leaf range once, linearly merge logical rows,
pack final page image(s) once, and publish split/separator/root changes once. Do not advance it to
FPGA or the full 500-block workload unless the 100-block discovery run reduces unique COW/page-touch
work and end-to-end wall clock while retaining cold-reopen state-root equality.
