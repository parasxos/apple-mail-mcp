"""Fixture-only regression checks for index retention and recovery."""
from __future__ import annotations

import multiprocessing
import sqlite3
import threading

import pytest

from email_mcp import fts
from email_mcp.fts import FtsIndex
from tests.test_fts import (
    EWS_MBOX_ROWID, _add_envelope_row, _add_envelope_row_in,
    _add_ews_mailbox, _doc_statuses, _write_body,
)


def _env_write(mail_dir, sql, args=()):
    with sqlite3.connect(mail_dir / 'MailData' / 'Envelope Index') as conn:
        row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def _index_write(sql, args=()):
    with sqlite3.connect(fts.db_path()) as conn:
        conn.execute(sql, args)
    conn.close()


def _mailbox_gap(mail_dir):
    row = _env_write(mail_dir, 'SELECT * FROM mailboxes WHERE ROWID=1')
    _env_write(mail_dir, 'DELETE FROM mailboxes WHERE ROWID=1')
    return row


def _restore_mailbox(mail_dir, row):
    _env_write(mail_dir, 'INSERT INTO mailboxes VALUES (?,?,?,?)', row)


@pytest.mark.parametrize('partial', [False, True])
def test_mailbox_join_gap_preserves_ledger_body_and_source(mail_fixture, partial):
    rowid = 500 if partial else 200
    if partial:
        _add_envelope_row(mail_fixture, rowid)
        _write_body(mail_fixture, rowid, 'partialuniqueterm', partial=True)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _index_write("UPDATE docs SET last_attempt=0, source='graph_miss' WHERE rowid=?", (rowid,))
    mailbox = _mailbox_gap(mail_fixture)
    assert rowid in idx._envelope_rowid_set()
    assert idx._envelope_urls_for([rowid]) == {}
    out = idx.incremental()
    assert out['removed'] == 0
    assert out['deferred'] == 1
    assert _doc_statuses()[rowid] == ('partial' if partial else 'missing')
    with sqlite3.connect(fts.db_path()) as conn:
        assert conn.execute('SELECT source, attempts FROM docs WHERE rowid=?', (rowid,)).fetchone() == ('graph_miss', 2)
    conn.close()
    if partial:
        assert idx.rowids_matching('partialuniqueterm') == [rowid]
    _restore_mailbox(mail_fixture, mailbox)
    _write_body(mail_fixture, rowid, 'downloadeduniqueterm')
    _index_write('UPDATE docs SET last_attempt=0 WHERE rowid=?', (rowid,))
    assert idx.incremental()['indexed'] == 1
    assert idx.rowids_matching('downloadeduniqueterm') == [rowid]
    assert idx.status()['cleanup']['retry_deferred_total'] == 1


def test_reconcile_locks_before_snapshot_and_newer_writer_survives(mail_fixture, monkeypatch):
    _add_ews_mailbox(mail_fixture)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    original = idx._envelope_rowid_set
    attempted, finished = threading.Event(), threading.Event()
    errors = []

    def write_new_body():
        try:
            with sqlite3.connect(fts.db_path(), timeout=5) as writer:
                attempted.set()
                writer.execute("INSERT INTO docs VALUES (600,'indexed',1,1,14,'graph')")
                writer.execute("INSERT INTO body_fts(rowid,body) VALUES (600,'remotebodyterm')")
                writer.execute("UPDATE meta SET value='600' WHERE key='last_rowid'")
            writer.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    writers = []

    def snapshot_with_competing_write():
        snapshot = original()
        _add_envelope_row_in(mail_fixture, 600, EWS_MBOX_ROWID, remote_id='remote600')
        writer = threading.Thread(target=write_new_body)
        writers.append(writer)
        writer.start()
        assert attempted.wait(2)
        assert not finished.wait(.05)
        return snapshot

    monkeypatch.setattr(idx, '_envelope_rowid_set', snapshot_with_competing_write)
    assert idx.reconcile()['removed'] == 0
    for writer in writers:
        writer.join(5)
        assert not writer.is_alive()
    assert not errors
    assert 600 in original()
    assert idx.backfilled_text(600) == 'remotebodyterm'
    assert idx.status()['cleanup']['pending_removal'] == 0


def test_uncommitted_mailbox_changes_are_not_visible(mail_fixture):
    writer = sqlite3.connect(mail_fixture / 'MailData' / 'Envelope Index')
    writer.execute('PRAGMA journal_mode=WAL')
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('DELETE FROM mailboxes WHERE ROWID=1')
        assert 200 in idx._envelope_urls_for([200])
    finally:
        writer.rollback()
        writer.close()


def test_absence_grace_persists_reappearance_restarts_it(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    now = [100000.0]
    monkeypatch.setattr(fts.time, 'time', lambda: now[0])
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=100')
    assert idx.reconcile()['pending_removal'] == 1
    now[0] += fts._ABSENCE_GRACE_SECONDS - 1
    assert FtsIndex(mail_base=mail_fixture).reconcile()['removed'] == 0
    _env_write(mail_fixture, 'UPDATE messages SET deleted=0 WHERE ROWID=100')
    assert idx.reconcile()['reappeared'] == 1
    assert idx.status()['cleanup']['pending_removal'] == 0
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=100')
    assert idx.reconcile()['removed'] == 0
    now[0] += 2
    assert idx.reconcile()['removed'] == 0
    now[0] += fts._ABSENCE_GRACE_SECONDS
    assert idx.reconcile()['removed'] == 1
    assert idx.rowids_matching('retracted') == []
    cleanup = FtsIndex(mail_base=mail_fixture).status()['cleanup']
    assert cleanup['removed_total'] == cleanup['reappeared_total'] == 1
    assert cleanup['last_removed_at'] and cleanup['last_reappeared_at']


def test_failed_envelope_scan_does_not_change_absence_or_counters(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=100')
    idx.reconcile()
    before = idx.status()['cleanup']

    def fail():
        raise sqlite3.OperationalError('incomplete snapshot')

    monkeypatch.setattr(idx, '_envelope_rowid_set', fail)
    with pytest.raises(sqlite3.OperationalError, match='incomplete snapshot'):
        idx.reconcile()
    assert idx.status()['cleanup'] == before
    assert idx.rowids_matching('retracted') == [100]


def test_reconcile_sql_failure_rolls_back_body_ledger_markers_and_counters(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=100')
    idx.reconcile()
    before = idx.status()['cleanup']
    _index_write("CREATE TRIGGER stop_cleanup BEFORE DELETE ON absence BEGIN SELECT RAISE(ABORT,'stop cleanup'); END")
    now = fts.time.time() + fts._ABSENCE_GRACE_SECONDS + 1
    monkeypatch.setattr(fts.time, 'time', lambda: now)
    with pytest.raises(sqlite3.IntegrityError, match='stop cleanup'):
        idx.reconcile()
    assert idx.rowids_matching('retracted') == [100]
    assert _doc_statuses()[100] == 'indexed'
    assert idx.status()['cleanup'] == before


def _interrupted_reconcile(mail_dir, db, ready):
    class Interrupted(FtsIndex):
        @staticmethod
        def _meta_set(cur, key, value):
            if key == 'last_reconcile_at':
                ready.set()
                threading.Event().wait(20)
            return FtsIndex._meta_set(cur, key, value)
    Interrupted(mail_base=mail_dir, db=db).reconcile()


def test_process_death_rolls_back_cleanup_transaction(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=100')
    idx.reconcile()
    _index_write('UPDATE absence SET first_absent=0, last_absent=1')
    before = idx.status()['cleanup']
    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    worker = ctx.Process(target=_interrupted_reconcile, args=(mail_fixture, fts.db_path(), ready))
    worker.start()
    try:
        assert ready.wait(8)
    finally:
        worker.terminate()
        worker.join(5)
    assert not worker.is_alive()
    assert idx.status()['cleanup'] == before
    assert idx.rowids_matching('retracted') == [100]


def test_recovered_ledger_hole_is_counted_once(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _index_write('DELETE FROM docs WHERE rowid=100')
    _index_write('DELETE FROM body_fts WHERE rowid=100')
    assert idx.reconcile()['recovered'] == 1
    assert idx.reconcile()['recovered'] == 0
    assert idx.rowids_matching('retracted') == [100]
    assert idx.status()['cleanup']['recovered_total'] == 1


def test_rebuild_retains_partial_across_join_gap_and_old_absence_deadline(mail_fixture, monkeypatch):
    _add_envelope_row(mail_fixture, 500)
    _write_body(mail_fixture, 500, 'retainedpartialword', partial=True)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    now = [100000.0]
    monkeypatch.setattr(fts.time, 'time', lambda: now[0])
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=100')
    idx.reconcile()
    mailbox = _mailbox_gap(mail_fixture)
    for _ in range(2):
        idx.rebuild()
    assert idx.rowids_matching('retainedpartialword') == [500]
    assert idx.rowids_matching('retracted') == [100]
    assert idx.status()['cleanup']['pending_removal'] == 1
    now[0] += fts._ABSENCE_GRACE_SECONDS + 1
    assert idx.reconcile()['removed'] == 1
    _restore_mailbox(mail_fixture, mailbox)
    assert idx.rowids_matching('retainedpartialword') == [500]


def test_rebuild_fresh_presence_clears_marker_and_fresh_partial_wins(mail_fixture):
    _add_envelope_row(mail_fixture, 500)
    _write_body(mail_fixture, 500, 'oldpartialword ' * 10, partial=True)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _env_write(mail_fixture, 'UPDATE messages SET deleted=1 WHERE ROWID=500')
    idx.reconcile()
    _env_write(mail_fixture, 'UPDATE messages SET deleted=0 WHERE ROWID=500')
    _write_body(mail_fixture, 500, 'newpartialword', partial=True)
    idx.rebuild()
    assert idx.rowids_matching('oldpartialword') == []
    assert idx.rowids_matching('newpartialword') == [500]
    cleanup = idx.status()['cleanup']
    assert cleanup['pending_removal'] == 0
    assert cleanup['reappeared_total'] == 1


@pytest.mark.parametrize('source', ['graph', 'local', 'removed', 'graph_miss'])
@pytest.mark.parametrize('result', ['hit', 'miss'])
@pytest.mark.parametrize('lane', ['graph', 'imap'])
def test_backfill_result_cannot_replace_newer_committed_evidence(mail_fixture, monkeypatch, source, result, lane):
    from tests.test_fts import _Ident, _ImapIdent, _write_imap_partial

    if lane == 'graph':
        _add_ews_mailbox(mail_fixture)
        _add_envelope_row_in(mail_fixture, 600, EWS_MBOX_ROWID, remote_id='remote600')
    else:
        _add_envelope_row_in(mail_fixture, 600, 2)
        _write_imap_partial(mail_fixture, 600, '<600@imap.test>')
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [_Ident()] if lane == 'graph' else [])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [_ImapIdent()] if lane == 'imap' else [])
    monkeypatch.setattr(FtsIndex, '_translate_ews_ids', lambda *a: {'remote600': 'rest600'})

    def competing_commit(*args):
        with sqlite3.connect(fts.db_path()) as writer:
            writer.execute('DELETE FROM body_fts WHERE rowid=600')
            if source == 'removed':
                writer.execute('DELETE FROM docs WHERE rowid=600')
            elif source == 'graph_miss':
                writer.execute("UPDATE docs SET source='graph_miss' WHERE rowid=600")
            else:
                writer.execute("UPDATE docs SET source=?, status='indexed' WHERE rowid=600", (source,))
                writer.execute("INSERT INTO body_fts(rowid,body) VALUES (600,'newercommittedbody')")
        writer.close()
        return None if result == 'miss' else {'content': 'stalerequestbody', 'contentType': 'text'}

    method = '_fetch_remote_body_by_id' if lane == 'graph' else '_fetch_imap_body'
    monkeypatch.setattr(FtsIndex, method, competing_commit)
    out = idx.backfill()
    assert out['deferred'] == 1
    assert out['backfilled'] == out['misses'] == 0
    assert idx.rowids_matching('stalerequestbody') == []
    with sqlite3.connect(fts.db_path()) as conn:
        row = conn.execute('SELECT source FROM docs WHERE rowid=600').fetchone()
        assert row == (None if source == 'removed' else (source,))
    conn.close()
    if source in ('graph', 'local'):
        assert idx.rowids_matching('newercommittedbody') == [600]
    if source == 'graph':
        assert idx.backfilled_text(600) == 'newercommittedbody'


@pytest.mark.parametrize('classification', ['no_lane', 'no_remote_id', 'no_message_id'])
def test_classification_revalidates_candidate_before_recording_reason(mail_fixture, monkeypatch, classification):
    from tests.test_fts import _Ident, _write_ews_partial

    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 600, 1 if classification == 'no_lane' else EWS_MBOX_ROWID)
    if classification == 'no_message_id':
        _write_ews_partial(mail_fixture, 600, None)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [])

    def upgrade():
        with sqlite3.connect(fts.db_path()) as writer:
            writer.execute("UPDATE docs SET source='graph', status='indexed' WHERE rowid=600")
            writer.execute('DELETE FROM body_fts WHERE rowid=600')
            writer.execute("INSERT INTO body_fts(rowid,body) VALUES (600,'keptbody')")
        writer.close()

    if classification == 'no_message_id':
        def missing_identifier(*args):
            upgrade()
            return None
        monkeypatch.setattr(idx, '_message_id_from_partial', missing_identifier)
    else:
        original = idx._envelope_backfill_meta
        def metadata(rows):
            meta = original(rows)
            upgrade()
            return meta
        monkeypatch.setattr(idx, '_envelope_backfill_meta', metadata)
    out = idx.backfill()
    assert out['deferred'] == 1
    assert idx.backfilled_text(600) == 'keptbody'
    with sqlite3.connect(fts.db_path()) as conn:
        assert conn.execute('SELECT * FROM backfill_reasons WHERE rowid=600').fetchall() == []
    conn.close()


def test_changed_provider_fingerprint_defers_old_miss(mail_fixture, monkeypatch):
    from tests.test_fts import _Ident

    _add_ews_mailbox(mail_fixture)
    _add_envelope_row_in(mail_fixture, 600, EWS_MBOX_ROWID, remote_id='remote600')
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [])

    def translated(*args):
        _index_write("UPDATE meta SET value='graph:new;imap:' WHERE key='backfill_identities'")
        return {}

    monkeypatch.setattr(FtsIndex, '_translate_ews_ids', translated)
    out = idx.backfill()
    assert out['deferred'] == 1 and out['misses'] == 0
    assert idx.status()['recovery']['confirmed_misses'] == 0


def test_empty_mailbox_url_defers_existing_partial_body(mail_fixture):
    _add_envelope_row(mail_fixture, 500)
    _write_body(mail_fixture, 500, 'partialretainedword', partial=True)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _env_write(mail_fixture, "UPDATE mailboxes SET url='' WHERE ROWID=1")
    _index_write('UPDATE docs SET last_attempt=0 WHERE rowid=500')
    assert idx.incremental()['deferred'] == 1
    assert idx.rowids_matching('partialretainedword') == [500]
