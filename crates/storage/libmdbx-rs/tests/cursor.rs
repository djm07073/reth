#![allow(missing_docs)]
use reth_libmdbx::*;
use std::borrow::Cow;
use tempfile::tempdir;

#[test]
fn test_get() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();

    assert_eq!(None, txn.cursor(dbi).unwrap().first::<(), ()>().unwrap());

    txn.put(dbi, b"key1", b"val1", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val2", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key3", b"val3", WriteFlags::empty()).unwrap();

    let mut cursor = txn.cursor(dbi).unwrap();
    assert_eq!(cursor.first().unwrap(), Some((*b"key1", *b"val1")));
    assert_eq!(cursor.get_current().unwrap(), Some((*b"key1", *b"val1")));
    assert_eq!(cursor.next().unwrap(), Some((*b"key2", *b"val2")));
    assert_eq!(cursor.prev().unwrap(), Some((*b"key1", *b"val1")));
    assert_eq!(cursor.last().unwrap(), Some((*b"key3", *b"val3")));
    assert_eq!(cursor.set(b"key1").unwrap(), Some(*b"val1"));
    assert_eq!(cursor.set_key(b"key3").unwrap(), Some((*b"key3", *b"val3")));
    assert_eq!(cursor.set_range(b"key2\0").unwrap(), Some((*b"key3", *b"val3")));
}

#[test]
fn test_get_dup() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap().dbi();
    txn.put(dbi, b"key1", b"val1", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key1", b"val2", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key1", b"val3", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val1", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val2", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val3", WriteFlags::empty()).unwrap();

    let mut cursor = txn.cursor(dbi).unwrap();
    assert_eq!(cursor.first().unwrap(), Some((*b"key1", *b"val1")));
    assert_eq!(cursor.first_dup().unwrap(), Some(*b"val1"));
    assert_eq!(cursor.get_current().unwrap(), Some((*b"key1", *b"val1")));
    assert_eq!(cursor.next_nodup().unwrap(), Some((*b"key2", *b"val1")));
    assert_eq!(cursor.next().unwrap(), Some((*b"key2", *b"val2")));
    assert_eq!(cursor.prev().unwrap(), Some((*b"key2", *b"val1")));
    assert_eq!(cursor.next_dup().unwrap(), Some((*b"key2", *b"val2")));
    assert_eq!(cursor.next_dup().unwrap(), Some((*b"key2", *b"val3")));
    assert_eq!(cursor.next_dup::<(), ()>().unwrap(), None);
    assert_eq!(cursor.prev_dup().unwrap(), Some((*b"key2", *b"val2")));
    assert_eq!(cursor.last_dup().unwrap(), Some(*b"val3"));
    assert_eq!(cursor.prev_nodup().unwrap(), Some((*b"key1", *b"val3")));
    assert_eq!(cursor.next_dup::<(), ()>().unwrap(), None);
    assert_eq!(cursor.set(b"key1").unwrap(), Some(*b"val1"));
    assert_eq!(cursor.set(b"key2").unwrap(), Some(*b"val1"));
    assert_eq!(cursor.set_range(b"key1\0").unwrap(), Some((*b"key2", *b"val1")));
    assert_eq!(cursor.get_both(b"key1", b"val3").unwrap(), Some(*b"val3"));
    assert_eq!(cursor.get_both_range::<()>(b"key1", b"val4").unwrap(), None);
    assert_eq!(cursor.get_both_range(b"key2", b"val").unwrap(), Some(*b"val1"));

    assert_eq!(cursor.last().unwrap(), Some((*b"key2", *b"val3")));
    cursor.del(WriteFlags::empty()).unwrap();
    assert_eq!(cursor.last().unwrap(), Some((*b"key2", *b"val2")));
    cursor.del(WriteFlags::empty()).unwrap();
    assert_eq!(cursor.last().unwrap(), Some((*b"key2", *b"val1")));
    cursor.del(WriteFlags::empty()).unwrap();
    assert_eq!(cursor.last().unwrap(), Some((*b"key1", *b"val3")));
}

fn batch_value(index: u32, payload_len: usize, fill: u8) -> Vec<u8> {
    let mut value = vec![fill; 4 + payload_len];
    value[..4].copy_from_slice(&index.to_be_bytes());
    value
}

#[test]
fn test_mutate_batch_builds_final_leaf_for_mixed_variable_size_mutations() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();
    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap().dbi();
    let values = (0..500).map(|index| batch_value(index * 2, 8, 0)).collect::<Vec<_>>();
    for entry in &values {
        txn.put(dbi, b"outer-key", entry, WriteFlags::NO_DUP_DATA).unwrap();
    }
    txn.commit().unwrap();

    // Exercise a clean COW path and mix all logical operation kinds in one
    // ordered stream. The replacement is deliberately larger than its source.
    let txn = env.begin_rw_txn().unwrap();
    let after_100 = batch_value(200, 48, 0x11);
    let inserted_203 = batch_value(203, 19, 0x22);
    let mut cursor = txn.cursor(dbi).unwrap();
    let outcome = cursor
        .mutate_batch(
            b"outer-key",
            &[
                BatchMutation::Replace { before: &values[100], after: &after_100 },
                BatchMutation::Delete { before: &values[101] },
                BatchMutation::Upsert { after: &inserted_203 },
            ],
        )
        .unwrap();
    let BatchOutcome::Applied(result) = outcome else {
        panic!("expected the nested-leaf fast path, got {outcome:?}")
    };
    assert_eq!(result.mutations_applied, 3);
    assert_eq!(result.source_pages, 1);
    assert_eq!(result.destination_pages, result.source_pages);
    assert!(result.source_bytes > 0);
    assert_ne!(result.destination_bytes, result.source_bytes);
    assert!(cursor.get_both::<()>(b"outer-key", &after_100).unwrap().is_some());
    assert!(cursor.get_both::<()>(b"outer-key", &values[100]).unwrap().is_none());
    assert!(cursor.get_both::<()>(b"outer-key", &values[101]).unwrap().is_none());
    assert!(cursor.get_both::<()>(b"outer-key", &inserted_203).unwrap().is_some());
    drop(cursor);
    txn.commit().unwrap();

    let txn = env.begin_ro_txn().unwrap();
    let mut cursor = txn.cursor(dbi).unwrap();
    assert!(cursor.get_both::<()>(b"outer-key", &after_100).unwrap().is_some());
    assert!(cursor.get_both::<()>(b"outer-key", &inserted_203).unwrap().is_some());
}

#[test]
fn test_mutate_batch_page_full_fallback_is_atomic() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();
    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap().dbi();
    let values = (0..500).map(|index| batch_value(index * 2, 8, 0)).collect::<Vec<_>>();
    for entry in &values {
        txn.put(dbi, b"outer-key", entry, WriteFlags::NO_DUP_DATA).unwrap();
    }
    txn.commit().unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let oversized = batch_value(200, 7_000, 0x44);
    let mut cursor = txn.cursor(dbi).unwrap();
    let outcome = cursor
        .mutate_batch(
            b"outer-key",
            &[BatchMutation::Replace { before: &values[100], after: &oversized }],
        )
        .unwrap();
    assert_eq!(outcome, BatchOutcome::Unsupported(BatchFallbackReason::PageFull));
    assert!(cursor.get_both::<()>(b"outer-key", &values[100]).unwrap().is_some());
    assert!(cursor.get_both::<()>(b"outer-key", &oversized).unwrap().is_none());
}

#[test]
fn test_mutate_batch_peer_cursor_fallback_is_atomic() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();
    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap().dbi();
    let values = (0..500).map(|index| batch_value(index, 8, 0)).collect::<Vec<_>>();
    for entry in &values {
        txn.put(dbi, b"outer-key", entry, WriteFlags::NO_DUP_DATA).unwrap();
    }
    txn.commit().unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let mut peer = txn.cursor(dbi).unwrap();
    assert!(peer.get_both::<()>(b"outer-key", &values[100]).unwrap().is_some());
    let replacement = batch_value(100, 24, 0x77);
    let mut cursor = txn.cursor(dbi).unwrap();
    let outcome = cursor
        .mutate_batch(
            b"outer-key",
            &[BatchMutation::Replace { before: &values[100], after: &replacement }],
        )
        .unwrap();
    assert_eq!(outcome, BatchOutcome::Unsupported(BatchFallbackReason::PeerCursor));
    assert!(cursor.get_both::<()>(b"outer-key", &values[100]).unwrap().is_some());
    assert!(cursor.get_both::<()>(b"outer-key", &replacement).unwrap().is_none());
}

#[test]
fn test_mutate_batch_repeated_mixed_updates_in_one_write_transaction() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();
    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap().dbi();
    let values = (0..1_000).map(|index| batch_value(index * 2, 8, 0)).collect::<Vec<_>>();
    for entry in &values {
        txn.put(dbi, b"outer-key", entry, WriteFlags::NO_DUP_DATA).unwrap();
    }
    txn.commit().unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let mut cursor = txn.cursor(dbi).unwrap();
    let mut replacements = Vec::new();
    let mut insertions = Vec::new();
    for round in 0..50 {
        let base = round * 10;
        let after = batch_value((base * 2) as u32, 16 + round, 0x55);
        let inserted = batch_value((base * 2 + 3) as u32, 9 + round, 0x66);
        let outcome = cursor
            .mutate_batch(
                b"outer-key",
                &[
                    BatchMutation::Replace { before: &values[base], after: &after },
                    BatchMutation::Delete { before: &values[base + 1] },
                    BatchMutation::Upsert { after: &inserted },
                ],
            )
            .unwrap();
        if matches!(outcome, BatchOutcome::Unsupported(_)) {
            assert!(cursor.get_both::<()>(b"outer-key", &values[base]).unwrap().is_some());
            cursor.del(WriteFlags::CURRENT).unwrap();
            cursor.put(b"outer-key", &after, WriteFlags::NO_DUP_DATA).unwrap();
            assert!(cursor.get_both::<()>(b"outer-key", &values[base + 1]).unwrap().is_some());
            cursor.del(WriteFlags::CURRENT).unwrap();
            cursor.put(b"outer-key", &inserted, WriteFlags::NO_DUP_DATA).unwrap();
        }
        replacements.push(after);
        insertions.push(inserted);
    }
    drop(cursor);
    txn.commit().unwrap();

    let txn = env.begin_ro_txn().unwrap();
    let mut cursor = txn.cursor(dbi).unwrap();
    for round in 0..50 {
        let base = round * 10;
        assert!(cursor.get_both::<()>(b"outer-key", &values[base]).unwrap().is_none());
        assert!(cursor.get_both::<()>(b"outer-key", &values[base + 1]).unwrap().is_none());
        assert!(cursor.get_both::<()>(b"outer-key", &replacements[round]).unwrap().is_some());
        assert!(cursor.get_both::<()>(b"outer-key", &insertions[round]).unwrap().is_some());
    }
}

#[test]
fn test_get_dupfixed() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let dbi =
        txn.create_db(None, DatabaseFlags::DUP_SORT | DatabaseFlags::DUP_FIXED).unwrap().dbi();
    txn.put(dbi, b"key1", b"val1", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key1", b"val2", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key1", b"val3", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val4", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val5", WriteFlags::empty()).unwrap();
    txn.put(dbi, b"key2", b"val6", WriteFlags::empty()).unwrap();

    let mut cursor = txn.cursor(dbi).unwrap();
    assert_eq!(cursor.first().unwrap(), Some((*b"key1", *b"val1")));
    assert_eq!(cursor.get_multiple().unwrap(), Some(*b"val1val2val3"));
    assert_eq!(cursor.next_multiple::<(), ()>().unwrap(), None);
}

#[test]
fn test_iter() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let items: Vec<(_, _)> = vec![
        (*b"key1", *b"val1"),
        (*b"key2", *b"val2"),
        (*b"key3", *b"val3"),
        (*b"key5", *b"val5"),
    ];

    {
        let txn = env.begin_rw_txn().unwrap();
        let db = txn.open_db(None).unwrap();
        for (key, data) in &items {
            txn.put(db.dbi(), key, data, WriteFlags::empty()).unwrap();
        }
        txn.commit().unwrap();
    }

    let txn = env.begin_ro_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();
    let mut cursor = txn.cursor(dbi).unwrap();

    // Because Result implements FromIterator, we can collect the iterator
    // of items of type Result<_, E> into a Result<Vec<_, E>> by specifying
    // the collection type via the turbofish syntax.
    assert_eq!(items, cursor.iter().collect::<Result<Vec<_>>>().unwrap());

    // Alternately, we can collect it into an appropriately typed variable.
    let retr: Result<Vec<_>> = cursor.iter_start().collect();
    assert_eq!(items, retr.unwrap());

    cursor.set::<()>(b"key2").unwrap();
    assert_eq!(
        items.clone().into_iter().skip(2).collect::<Vec<_>>(),
        cursor.iter().collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(items, cursor.iter_start().collect::<Result<Vec<_>>>().unwrap());

    assert_eq!(
        items.clone().into_iter().skip(1).collect::<Vec<_>>(),
        cursor.iter_from(b"key2").collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(
        items.into_iter().skip(3).collect::<Vec<_>>(),
        cursor.iter_from(b"key4").collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(
        Vec::<((), ())>::new(),
        cursor.iter_from(b"key6").collect::<Result<Vec<_>>>().unwrap()
    );
}

#[test]
fn test_iter_empty_database() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();
    let txn = env.begin_ro_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();
    let mut cursor = txn.cursor(dbi).unwrap();

    assert!(cursor.iter::<(), ()>().next().is_none());
    assert!(cursor.iter_start::<(), ()>().next().is_none());
    assert!(cursor.iter_from::<(), ()>(b"foo").next().is_none());
}

#[test]
fn test_iter_empty_dup_database() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let txn = env.begin_rw_txn().unwrap();
    txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap();
    txn.commit().unwrap();

    let txn = env.begin_ro_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();
    let mut cursor = txn.cursor(dbi).unwrap();

    assert!(cursor.iter::<(), ()>().next().is_none());
    assert!(cursor.iter_start::<(), ()>().next().is_none());
    assert!(cursor.iter_from::<(), ()>(b"foo").next().is_none());
    assert!(cursor.iter_from::<(), ()>(b"foo").next().is_none());
    assert!(cursor.iter_dup::<(), ()>().flatten().next().is_none());
    assert!(cursor.iter_dup_start::<(), ()>().flatten().next().is_none());
    assert!(cursor.iter_dup_from::<(), ()>(b"foo").flatten().next().is_none());
    assert!(cursor.iter_dup_of::<(), ()>(b"foo").next().is_none());
}

#[test]
fn test_iter_dup() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let txn = env.begin_rw_txn().unwrap();
    txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap();
    txn.commit().unwrap();

    let items: Vec<(_, _)> = [
        (b"a", b"1"),
        (b"a", b"2"),
        (b"a", b"3"),
        (b"b", b"1"),
        (b"b", b"2"),
        (b"b", b"3"),
        (b"c", b"1"),
        (b"c", b"2"),
        (b"c", b"3"),
        (b"e", b"1"),
        (b"e", b"2"),
        (b"e", b"3"),
    ]
    .iter()
    .map(|&(&k, &v)| (k, v))
    .collect();

    {
        let txn = env.begin_rw_txn().unwrap();
        for (key, data) in items.clone() {
            let db = txn.open_db(None).unwrap();
            txn.put(db.dbi(), key, data, WriteFlags::empty()).unwrap();
        }
        txn.commit().unwrap();
    }

    let txn = env.begin_ro_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();
    let mut cursor = txn.cursor(dbi).unwrap();
    assert_eq!(items, cursor.iter_dup().flatten().collect::<Result<Vec<_>>>().unwrap());

    cursor.set::<()>(b"b").unwrap();
    assert_eq!(
        items.iter().copied().skip(4).collect::<Vec<_>>(),
        cursor.iter_dup().flatten().collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(items, cursor.iter_dup_start().flatten().collect::<Result<Vec<_>>>().unwrap());

    assert_eq!(
        items.iter().copied().skip(3).collect::<Vec<_>>(),
        cursor.iter_dup_from(b"b").flatten().collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(
        items.iter().copied().skip(3).collect::<Vec<_>>(),
        cursor.iter_dup_from(b"ab").flatten().collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(
        items.iter().copied().skip(9).collect::<Vec<_>>(),
        cursor.iter_dup_from(b"d").flatten().collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(
        Vec::<([u8; 1], [u8; 1])>::new(),
        cursor.iter_dup_from(b"f").flatten().collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(
        items.iter().copied().skip(3).take(3).collect::<Vec<_>>(),
        cursor.iter_dup_of(b"b").collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(0, cursor.iter_dup_of::<(), ()>(b"foo").count());
}

#[test]
fn test_iter_del_get() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let items = vec![(*b"a", *b"1"), (*b"b", *b"2")];
    {
        let txn = env.begin_rw_txn().unwrap();
        let dbi = txn.create_db(None, DatabaseFlags::DUP_SORT).unwrap().dbi();
        assert_eq!(
            txn.cursor(dbi)
                .unwrap()
                .iter_dup_of::<(), ()>(b"a")
                .collect::<Result<Vec<_>>>()
                .unwrap()
                .len(),
            0
        );
        txn.commit().unwrap();
    }

    {
        let txn = env.begin_rw_txn().unwrap();
        let db = txn.open_db(None).unwrap();
        for (key, data) in &items {
            txn.put(db.dbi(), key, data, WriteFlags::empty()).unwrap();
        }
        txn.commit().unwrap();
    }

    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();
    let mut cursor = txn.cursor(dbi).unwrap();
    assert_eq!(items, cursor.iter_dup().flatten().collect::<Result<Vec<_>>>().unwrap());

    assert_eq!(
        items.iter().copied().take(1).collect::<Vec<(_, _)>>(),
        cursor.iter_dup_of(b"a").collect::<Result<Vec<_>>>().unwrap()
    );

    assert_eq!(cursor.set(b"a").unwrap(), Some(*b"1"));

    cursor.del(WriteFlags::empty()).unwrap();

    assert_eq!(cursor.iter_dup_of::<(), ()>(b"a").collect::<Result<Vec<_>>>().unwrap().len(), 0);
}

#[test]
fn test_put_del() {
    let dir = tempdir().unwrap();
    let env = Environment::builder().open(dir.path()).unwrap();

    let txn = env.begin_rw_txn().unwrap();
    let dbi = txn.open_db(None).unwrap().dbi();
    let mut cursor = txn.cursor(dbi).unwrap();

    cursor.put(b"key1", b"val1", WriteFlags::empty()).unwrap();
    cursor.put(b"key2", b"val2", WriteFlags::empty()).unwrap();
    cursor.put(b"key3", b"val3", WriteFlags::empty()).unwrap();

    assert_eq!(
        cursor.set_key(b"key2").unwrap(),
        Some((Cow::Borrowed(b"key2" as &[u8]), Cow::Borrowed(b"val2" as &[u8])))
    );
    assert_eq!(
        cursor.get_current().unwrap(),
        Some((Cow::Borrowed(b"key2" as &[u8]), Cow::Borrowed(b"val2" as &[u8])))
    );

    cursor.del(WriteFlags::empty()).unwrap();
    assert_eq!(
        cursor.get_current().unwrap(),
        Some((Cow::Borrowed(b"key3" as &[u8]), Cow::Borrowed(b"val3" as &[u8])))
    );
    assert_eq!(
        cursor.last().unwrap(),
        Some((Cow::Borrowed(b"key3" as &[u8]), Cow::Borrowed(b"val3" as &[u8])))
    );
}
