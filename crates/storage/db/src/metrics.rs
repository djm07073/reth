use crate::Tables;
use metrics::Histogram;
use quanta::Instant;
use reth_metrics::{metrics::Counter, Metrics};
use rustc_hash::FxHashMap;
use std::time::Duration;
use strum::{EnumCount, EnumIter, IntoEnumIterator};

/// Caches metric handles for database environment to make sure handles are not re-created
/// on every operation.
///
/// Requires a metric recorder to be registered before creating an instance of this struct.
/// Otherwise, metric recording will no-op.
#[derive(Debug)]
pub(crate) struct DatabaseEnvMetrics {
    /// Caches `OperationMetrics` handles for each table and operation tuple.
    operations: FxHashMap<(&'static str, Operation), OperationMetrics>,
    /// Caches `TransactionMetrics` handles for counters grouped by only transaction mode.
    /// Updated both at tx open and close.
    transactions: FxHashMap<TransactionMode, TransactionMetrics>,
    /// Caches `TransactionOutcomeMetrics` handles for counters grouped by transaction mode and
    /// outcome. Can only be updated at tx close, as outcome is only known at that point.
    transaction_outcomes:
        FxHashMap<(TransactionMode, TransactionOutcome), TransactionOutcomeMetrics>,
}

impl DatabaseEnvMetrics {
    pub(crate) fn new() -> Self {
        // Pre-populate metric handle maps with all possible combinations of labels
        // to avoid runtime locks on the map when recording metrics.
        Self {
            operations: Self::generate_operation_handles(),
            transactions: Self::generate_transaction_handles(),
            transaction_outcomes: Self::generate_transaction_outcome_handles(),
        }
    }

    /// Generate a map of all possible operation handles for each table and operation tuple.
    /// Used for tracking all operation metrics.
    fn generate_operation_handles() -> FxHashMap<(&'static str, Operation), OperationMetrics> {
        let mut operations = FxHashMap::with_capacity_and_hasher(
            Tables::COUNT * Operation::COUNT,
            Default::default(),
        );
        for table in Tables::ALL {
            for operation in Operation::iter() {
                operations.insert(
                    (table.name(), operation),
                    OperationMetrics::new_with_labels(&[
                        (Labels::Table.as_str(), table.name()),
                        (Labels::Operation.as_str(), operation.as_str()),
                    ]),
                );
            }
        }
        operations
    }

    /// Generate a map of all possible transaction modes to metric handles.
    /// Used for tracking a counter of open transactions.
    fn generate_transaction_handles() -> FxHashMap<TransactionMode, TransactionMetrics> {
        TransactionMode::iter()
            .map(|mode| {
                (
                    mode,
                    TransactionMetrics::new_with_labels(&[(
                        Labels::TransactionMode.as_str(),
                        mode.as_str(),
                    )]),
                )
            })
            .collect()
    }

    /// Generate a map of all possible transaction mode and outcome handles.
    /// Used for tracking various stats for finished transactions (e.g. commit duration).
    fn generate_transaction_outcome_handles(
    ) -> FxHashMap<(TransactionMode, TransactionOutcome), TransactionOutcomeMetrics> {
        let mut transaction_outcomes = FxHashMap::with_capacity_and_hasher(
            TransactionMode::COUNT * TransactionOutcome::COUNT,
            Default::default(),
        );
        for mode in TransactionMode::iter() {
            for outcome in TransactionOutcome::iter() {
                transaction_outcomes.insert(
                    (mode, outcome),
                    TransactionOutcomeMetrics::new_with_labels(&[
                        (Labels::TransactionMode.as_str(), mode.as_str()),
                        (Labels::TransactionOutcome.as_str(), outcome.as_str()),
                    ]),
                );
            }
        }
        transaction_outcomes
    }

    /// Record a metric for database operation executed in `f`.
    /// Panics if a metric recorder is not found for the given table and operation.
    pub(crate) fn record_operation<R>(
        &self,
        table: &'static str,
        operation: Operation,
        value_size: Option<usize>,
        f: impl FnOnce() -> R,
    ) -> R {
        if let Some(metrics) = self.operations.get(&(table, operation)) {
            metrics.record(value_size, f)
        } else {
            f()
        }
    }

    /// Record bytes returned by a database operation after it completes.
    pub(crate) fn record_operation_result_bytes(
        &self,
        table: &'static str,
        operation: Operation,
        result_size: usize,
    ) {
        if let Some(metrics) = self.operations.get(&(table, operation)) {
            metrics.record_result_bytes(result_size);
        }
    }

    /// Record time and bytes spent serializing a value before a database write.
    pub(crate) fn record_serialization(
        &self,
        table: &'static str,
        operation: Operation,
        duration: Duration,
        serialized_size: usize,
    ) {
        if let Some(metrics) = self.operations.get(&(table, operation)) {
            metrics.record_serialization(duration, serialized_size);
        }
    }

    /// Record page-batch work independently of the generic operation latency metrics.
    pub(crate) fn record_page_batch_applied(
        &self,
        table: &'static str,
        replacements: usize,
        source_pages: usize,
        destination_pages: usize,
        source_bytes: usize,
        destination_bytes: usize,
    ) {
        metrics::counter!("database.page_batch.attempts_total", "table" => table).increment(1);
        metrics::counter!("database.page_batch.applied_total", "table" => table).increment(1);
        metrics::counter!("database.page_batch.replacements_total", "table" => table)
            .increment(replacements as u64);
        metrics::counter!("database.page_batch.source_pages_total", "table" => table)
            .increment(source_pages as u64);
        metrics::counter!("database.page_batch.destination_pages_total", "table" => table)
            .increment(destination_pages as u64);
        metrics::counter!("database.page_batch.source_bytes_total", "table" => table)
            .increment(source_bytes as u64);
        metrics::counter!("database.page_batch.destination_bytes_total", "table" => table)
            .increment(destination_bytes as u64);
    }

    /// Record an atomic page-batch fallback, labelled by its bounded reason.
    pub(crate) fn record_page_batch_fallback(&self, table: &'static str, reason: &'static str) {
        metrics::counter!("database.page_batch.attempts_total", "table" => table).increment(1);
        metrics::counter!(
            "database.page_batch.fallback_total",
            "table" => table,
            "reason" => reason
        )
        .increment(1);
    }

    /// Record metrics for opening a database transaction.
    pub(crate) fn record_opened_transaction(&self, mode: TransactionMode) {
        self.transactions
            .get(&mode)
            .expect("transaction mode metric handle not found")
            .record_open();
    }

    /// Record metrics for closing a database transactions.
    #[cfg(feature = "mdbx")]
    pub(crate) fn record_closed_transaction(
        &self,
        mode: TransactionMode,
        outcome: TransactionOutcome,
        open_duration: Duration,
        close_duration: Option<Duration>,
        commit_latency: Option<reth_libmdbx::CommitLatency>,
        transaction_space: Option<(u64, u64)>,
        page_ops: Option<[u64; 12]>,
    ) {
        self.transactions
            .get(&mode)
            .expect("transaction mode metric handle not found")
            .record_close();

        self.transaction_outcomes
            .get(&(mode, outcome))
            .expect("transaction outcome metric handle not found")
            .record(open_duration, close_duration, commit_latency, transaction_space, page_ops);
    }
}

/// Transaction mode for the database, either read-only or read-write.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, EnumCount, EnumIter)]
pub(crate) enum TransactionMode {
    /// Read-only transaction mode.
    ReadOnly,
    /// Read-write transaction mode.
    ReadWrite,
}

impl TransactionMode {
    /// Returns the transaction mode as a string.
    pub(crate) const fn as_str(&self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::ReadWrite => "read-write",
        }
    }

    /// Returns `true` if the transaction mode is read-only.
    pub(crate) const fn is_read_only(&self) -> bool {
        matches!(self, Self::ReadOnly)
    }
}

/// Transaction outcome after a database operation - commit, abort, or drop.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, EnumCount, EnumIter)]
pub(crate) enum TransactionOutcome {
    /// Successful commit of the transaction.
    Commit,
    /// Aborted transaction.
    Abort,
    /// Dropped transaction.
    Drop,
}

impl TransactionOutcome {
    /// Returns the transaction outcome as a string.
    pub(crate) const fn as_str(&self) -> &'static str {
        match self {
            Self::Commit => "commit",
            Self::Abort => "abort",
            Self::Drop => "drop",
        }
    }

    /// Returns `true` if the transaction outcome is a commit.
    pub(crate) const fn is_commit(&self) -> bool {
        matches!(self, Self::Commit)
    }
}

/// Types of operations conducted on the database: get, put, delete, and various cursor operations.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, EnumCount, EnumIter)]
pub(crate) enum Operation {
    /// Database get operation.
    Get,
    /// Database put upsert operation.
    PutUpsert,
    /// Database put append operation.
    PutAppend,
    /// Database delete operation.
    Delete,
    /// Database cursor upsert operation.
    CursorUpsert,
    /// Database cursor update-current operation.
    CursorUpdateCurrent,
    /// Database duplicate page-batch replacement operation.
    CursorBatchReplace,
    /// Database cursor insert operation.
    CursorInsert,
    /// Database cursor append operation.
    CursorAppend,
    /// Database cursor append duplicates operation.
    CursorAppendDup,
    /// Database cursor delete current operation.
    CursorDeleteCurrent,
    /// Database cursor delete current duplicates operation.
    CursorDeleteCurrentDuplicates,
    /// Database cursor exact seek operation.
    CursorSeekExact,
    /// Database cursor range seek operation.
    CursorSeek,
    /// Database duplicate cursor seek by key and subkey operation.
    CursorSeekByKeySubkey,
}

impl Operation {
    /// Returns the operation as a string.
    pub(crate) const fn as_str(&self) -> &'static str {
        match self {
            Self::Get => "get",
            Self::PutUpsert => "put-upsert",
            Self::PutAppend => "put-append",
            Self::Delete => "delete",
            Self::CursorUpsert => "cursor-upsert",
            Self::CursorUpdateCurrent => "cursor-update-current",
            Self::CursorBatchReplace => "cursor-batch-replace",
            Self::CursorInsert => "cursor-insert",
            Self::CursorAppend => "cursor-append",
            Self::CursorAppendDup => "cursor-append-dup",
            Self::CursorDeleteCurrent => "cursor-delete-current",
            Self::CursorDeleteCurrentDuplicates => "cursor-delete-current-duplicates",
            Self::CursorSeekExact => "cursor-seek-exact",
            Self::CursorSeek => "cursor-seek",
            Self::CursorSeekByKeySubkey => "cursor-seek-by-key-subkey",
        }
    }
}

/// Enum defining labels for various aspects used in metrics.
enum Labels {
    /// Label representing a table.
    Table,
    /// Label representing a transaction mode.
    TransactionMode,
    /// Label representing a transaction outcome.
    TransactionOutcome,
    /// Label representing a database operation.
    Operation,
}

impl Labels {
    /// Converts each label variant into its corresponding string representation.
    pub(crate) const fn as_str(&self) -> &'static str {
        match self {
            Self::Table => "table",
            Self::TransactionMode => "mode",
            Self::TransactionOutcome => "outcome",
            Self::Operation => "operation",
        }
    }
}

#[derive(Metrics, Clone)]
#[metrics(scope = "database.transaction")]
pub(crate) struct TransactionMetrics {
    /// Total number of opened database transactions (cumulative)
    opened_total: Counter,
    /// Total number of closed database transactions (cumulative)
    closed_total: Counter,
}

impl TransactionMetrics {
    pub(crate) fn record_open(&self) {
        self.opened_total.increment(1);
    }

    pub(crate) fn record_close(&self) {
        self.closed_total.increment(1);
    }
}

#[derive(Metrics, Clone)]
#[metrics(scope = "database.transaction")]
pub(crate) struct TransactionOutcomeMetrics {
    /// The time a database transaction has been open
    open_duration_seconds: Histogram,
    /// The time it took to close a database transaction
    close_duration_seconds: Histogram,
    /// The time it took to prepare a transaction commit
    commit_preparation_duration_seconds: Histogram,
    /// Duration of GC update during transaction commit by wall clock
    commit_gc_wallclock_duration_seconds: Histogram,
    /// The time it took to conduct audit of a transaction commit
    commit_audit_duration_seconds: Histogram,
    /// The time it took to write dirty/modified data pages to a filesystem during transaction
    /// commit
    commit_write_duration_seconds: Histogram,
    /// The time it took to sync written data to the disk/storage during transaction commit
    commit_sync_duration_seconds: Histogram,
    /// The time it took to release resources during transaction commit
    commit_ending_duration_seconds: Histogram,
    /// The total duration of a transaction commit
    commit_whole_duration_seconds: Histogram,
    /// User-mode CPU time spent on GC update during transaction commit
    commit_gc_cputime_duration_seconds: Histogram,
    /// Dirty bytes accumulated by a write transaction immediately before commit
    dirty_bytes: Histogram,
    /// Bytes retired by copy-on-write in a write transaction immediately before commit
    retired_bytes: Histogram,
    /// New MDBX pages allocated during the transaction
    page_newly_total: Counter,
    /// MDBX pages copied on write during the transaction
    page_cow_total: Counter,
    /// Parent dirty pages cloned during the transaction
    page_clone_total: Counter,
    /// MDBX page splits during the transaction
    page_split_total: Counter,
    /// MDBX page merges during the transaction
    page_merge_total: Counter,
    /// MDBX dirty pages spilled during the transaction
    page_spill_total: Counter,
    /// MDBX pages reloaded after spill during the transaction
    page_unspill_total: Counter,
    /// MDBX explicit disk write operations during the transaction
    page_wops_total: Counter,
    /// MDBX explicit msync operations during the transaction
    page_msync_total: Counter,
    /// MDBX explicit fsync operations during the transaction
    page_fsync_total: Counter,
    /// MDBX prefault write operations during the transaction
    page_prefault_total: Counter,
    /// MDBX mincore calls during the transaction
    page_mincore_total: Counter,
}

impl TransactionOutcomeMetrics {
    /// Record transaction closing with the duration it was open and the duration it took to close
    /// it.
    #[cfg(feature = "mdbx")]
    pub(crate) fn record(
        &self,
        open_duration: Duration,
        close_duration: Option<Duration>,
        commit_latency: Option<reth_libmdbx::CommitLatency>,
        transaction_space: Option<(u64, u64)>,
        page_ops: Option<[u64; 12]>,
    ) {
        self.open_duration_seconds.record(open_duration);

        if let Some(close_duration) = close_duration {
            self.close_duration_seconds.record(close_duration)
        }

        if let Some(commit_latency) = commit_latency {
            self.commit_preparation_duration_seconds.record(commit_latency.preparation());
            self.commit_gc_wallclock_duration_seconds.record(commit_latency.gc_wallclock());
            self.commit_audit_duration_seconds.record(commit_latency.audit());
            self.commit_write_duration_seconds.record(commit_latency.write());
            self.commit_sync_duration_seconds.record(commit_latency.sync());
            self.commit_ending_duration_seconds.record(commit_latency.ending());
            self.commit_whole_duration_seconds.record(commit_latency.whole());
            self.commit_gc_cputime_duration_seconds.record(commit_latency.gc_cputime());
        }

        if let Some((dirty_bytes, retired_bytes)) = transaction_space {
            self.dirty_bytes.record(dirty_bytes as f64);
            self.retired_bytes.record(retired_bytes as f64);
        }

        if let Some(
            [newly, cow, clone, split, merge, spill, unspill, wops, msync, fsync, prefault, mincore],
        ) = page_ops
        {
            self.page_newly_total.increment(newly);
            self.page_cow_total.increment(cow);
            self.page_clone_total.increment(clone);
            self.page_split_total.increment(split);
            self.page_merge_total.increment(merge);
            self.page_spill_total.increment(spill);
            self.page_unspill_total.increment(unspill);
            self.page_wops_total.increment(wops);
            self.page_msync_total.increment(msync);
            self.page_fsync_total.increment(fsync);
            self.page_prefault_total.increment(prefault);
            self.page_mincore_total.increment(mincore);
        }
    }
}

#[derive(Metrics, Clone)]
#[metrics(scope = "database.operation")]
pub(crate) struct OperationMetrics {
    /// Total number of database operations made
    calls_total: Counter,
    /// Wall-clock duration of every profiled database operation.
    duration_seconds: Histogram,
    /// Encoded input bytes supplied to the operation. For delete-current this is the last observed
    /// matched value size.
    logical_bytes_total: Counter,
    /// Encoded bytes returned by seek operations.
    result_bytes_total: Counter,
    /// Wall-clock duration of value serialization before a database write.
    serialization_duration_seconds: Histogram,
    /// Encoded bytes produced by serialization before a database write.
    serialized_bytes_total: Counter,
}

impl OperationMetrics {
    /// Record operation metric.
    pub(crate) fn record<R>(&self, value_size: Option<usize>, f: impl FnOnce() -> R) -> R {
        self.calls_total.increment(1);
        if let Some(value_size) = value_size {
            self.logical_bytes_total.increment(value_size as u64);
        }
        let start = Instant::now();
        let result = f();
        self.duration_seconds.record(start.elapsed());
        result
    }

    /// Record encoded result bytes from a completed operation.
    pub(crate) fn record_result_bytes(&self, result_size: usize) {
        self.result_bytes_total.increment(result_size as u64);
    }

    /// Record serialization work performed before a database write.
    pub(crate) fn record_serialization(&self, duration: Duration, serialized_size: usize) {
        self.serialization_duration_seconds.record(duration);
        self.serialized_bytes_total.increment(serialized_size as u64);
    }
}
