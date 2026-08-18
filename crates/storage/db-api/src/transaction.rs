use crate::{
    cursor::{DbCursorRO, DbCursorRW, DbDupCursorRO, DbDupCursorRW},
    table::{DupSort, Encode, Table},
    DatabaseError,
};
use std::fmt::Debug;

/// Cumulative MDBX-style page-operation counters exposed by a database transaction.
///
/// Backends that do not use page-based copy-on-write storage return `None` from
/// [`DbTx::page_ops`].
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct DatabasePageOps {
    /// Pages allocated for new tree content.
    pub newly: u64,
    /// Frozen pages copied for modification.
    pub cow: u64,
    /// Parent-transaction dirty pages cloned by a nested transaction.
    pub clone: u64,
    /// Page splits.
    pub split: u64,
    /// Page merges.
    pub merge: u64,
    /// Dirty pages spilled before commit.
    pub spill: u64,
    /// Previously spilled pages restored for modification.
    pub unspill: u64,
}

impl DatabasePageOps {
    /// Returns a monotonic-counter delta without underflowing if an environment is reopened.
    pub fn saturating_sub(self, earlier: Self) -> Self {
        Self {
            newly: self.newly.saturating_sub(earlier.newly),
            cow: self.cow.saturating_sub(earlier.cow),
            clone: self.clone.saturating_sub(earlier.clone),
            split: self.split.saturating_sub(earlier.split),
            merge: self.merge.saturating_sub(earlier.merge),
            spill: self.spill.saturating_sub(earlier.spill),
            unspill: self.unspill.saturating_sub(earlier.unspill),
        }
    }
}

/// Helper adapter type for accessing [`DbTx`] cursor.
pub type CursorTy<TX, T> = <TX as DbTx>::Cursor<T>;

/// Helper adapter type for accessing [`DbTx`] dup cursor.
pub type DupCursorTy<TX, T> = <TX as DbTx>::DupCursor<T>;

/// Helper adapter type for accessing [`DbTxMut`] mutable cursor.
pub type CursorMutTy<TX, T> = <TX as DbTxMut>::CursorMut<T>;

/// Helper adapter type for accessing [`DbTxMut`] mutable dup cursor.
pub type DupCursorMutTy<TX, T> = <TX as DbTxMut>::DupCursorMut<T>;

/// Read only transaction
pub trait DbTx: Debug + Send {
    /// Cursor type for this read-only transaction
    type Cursor<T: Table>: DbCursorRO<T> + Send;
    /// `DupCursor` type for this read-only transaction
    type DupCursor<T: DupSort>: DbDupCursorRO<T> + DbCursorRO<T> + Send;

    /// Get value by an owned key
    fn get<T: Table>(&self, key: T::Key) -> Result<Option<T::Value>, DatabaseError>;
    /// Get value by a reference to the encoded key, especially useful for "raw" keys
    /// that encode to themselves like Address and B256. Doesn't need to clone a
    /// reference key like `get`.
    fn get_by_encoded_key<T: Table>(
        &self,
        key: &<T::Key as Encode>::Encoded,
    ) -> Result<Option<T::Value>, DatabaseError>;
    /// Commit for read only transaction will consume and free transaction and allows
    /// freeing of memory pages
    fn commit(self) -> Result<(), DatabaseError>;
    /// Aborts transaction
    fn abort(self);
    /// Iterate over read only values in table.
    fn cursor_read<T: Table>(&self) -> Result<Self::Cursor<T>, DatabaseError>;
    /// Iterate over read only values in dup sorted table.
    fn cursor_dup_read<T: DupSort>(&self) -> Result<Self::DupCursor<T>, DatabaseError>;
    /// Returns number of entries in the table.
    fn entries<T: Table>(&self) -> Result<usize, DatabaseError>;
    /// Disables long-lived read transaction safety guarantees.
    fn disable_long_read_transaction_safety(&mut self);

    /// Returns cumulative page-operation counters when supported by the backend.
    fn page_ops(&self) -> Option<DatabasePageOps> {
        None
    }
}

/// Read write transaction that allows writing to database
pub trait DbTxMut: Send {
    /// Read-Write Cursor type
    type CursorMut<T: Table>: DbCursorRW<T> + DbCursorRO<T> + Send;
    /// Read-Write `DupCursor` type
    type DupCursorMut<T: DupSort>: DbDupCursorRW<T>
        + DbCursorRW<T>
        + DbDupCursorRO<T>
        + DbCursorRO<T>
        + Send;

    /// Put value to database
    fn put<T: Table>(&self, key: T::Key, value: T::Value) -> Result<(), DatabaseError>;
    /// Append value with the largest key to database. This should have the same
    /// outcome as `put`, but databases like MDBX provide dedicated modes to make
    /// it much faster, typically from O(logN) down to O(1) thanks to no lookup.
    fn append<T: Table>(&self, key: T::Key, value: T::Value) -> Result<(), DatabaseError> {
        self.put::<T>(key, value)
    }
    /// Delete value from database
    fn delete<T: Table>(&self, key: T::Key, value: Option<T::Value>)
        -> Result<bool, DatabaseError>;
    /// Clears database.
    fn clear<T: Table>(&self) -> Result<(), DatabaseError>;
    /// Cursor mut
    fn cursor_write<T: Table>(&self) -> Result<Self::CursorMut<T>, DatabaseError>;
    /// `DupCursor` mut.
    fn cursor_dup_write<T: DupSort>(&self) -> Result<Self::DupCursorMut<T>, DatabaseError>;
}

#[cfg(test)]
mod tests {
    use super::DatabasePageOps;

    #[test]
    fn page_ops_delta_saturates_reopened_environment_counters() {
        let before = DatabasePageOps {
            newly: 10,
            cow: 20,
            clone: 3,
            split: 4,
            merge: 5,
            spill: 6,
            unspill: 7,
        };
        let after = DatabasePageOps {
            newly: 15,
            cow: 18,
            clone: 5,
            split: 8,
            merge: 5,
            spill: 7,
            unspill: 9,
        };

        assert_eq!(
            after.saturating_sub(before),
            DatabasePageOps {
                newly: 5,
                cow: 0,
                clone: 2,
                split: 4,
                merge: 0,
                spill: 1,
                unspill: 2,
            }
        );
    }
}
