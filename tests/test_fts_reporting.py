"""Coverage reports use local evidence and remain useful without search hits."""
from __future__ import annotations

import sqlite3

import pytest

from email_mcp import doctor, fts
from email_mcp.fts import FtsIndex
from email_mcp.fts_reporting import coverage_report
from email_mcp.sources.apple_mail import AppleMailSource
from email_mcp.sources.base import SearchQuery
from tests.test_fts import (
    EWS_MBOX_ROWID, _Ident, _ImapIdent, _add_envelope_row,
    _add_envelope_row_in, _add_ews_mailbox, _write_body,
    _write_ews_partial, _write_imap_partial,
)


def _causes(report):
    return {item['cause'] for item in report['remedies']}


def _write(sql, args=()):
    with sqlite3.connect(fts.db_path()) as conn:
        conn.execute(sql, args)
    conn.close()


@pytest.fixture(autouse=True)
def _fixture_mail_path(mail_fixture, monkeypatch):
    monkeypatch.setenv('EMAIL_MCP_MAIL_DIR', str(mail_fixture))


@pytest.mark.parametrize('attempts,exhausted', [(5, 0), (6, 1)])
def test_retry_exhaustion_boundary_and_global_search_metadata(mail_fixture, monkeypatch, attempts, exhausted):
    _add_envelope_row(mail_fixture, 500)
    _write_body(mail_fixture, 500, 'searchablepartialword', partial=True)
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _write('UPDATE docs SET attempts=? WHERE rowid=200', (attempts,))
    monkeypatch.setattr(FtsIndex, 'incremental', lambda *a, **kw: {})
    for method in ('_graph_identities', '_imap_identities', '_fetch_remote_body', '_fetch_imap_body'):
        monkeypatch.setattr(FtsIndex, method, lambda *a: pytest.fail('reporting must stay local'))
    st = idx.status()
    assert st['backlog'] == 0
    assert st['docs']['local_retry_exhausted'] == exhausted
    assert st['coverage']['state'] == 'incomplete'
    assert 'body_gaps' in _causes(st)
    assert ('local_retry_exhausted' in _causes(st)) == bool(exhausted)
    src = AppleMailSource(mail_base=mail_fixture)
    try:
        assert [r.id for r in src.search(SearchQuery(query='searchablepartialword'))] == ['500']
        first = src.fts_status()
        assert src.search(SearchQuery(query='doesnotexistword', from_addr='nobody')) == []
        empty = src.fts_status()
        for report in (first, empty):
            assert report['partial'] == report['missing'] == 1
            assert report['coverage'] == st['coverage']
            assert report['remedies'] == st['remedies']
            assert report['local_retry_exhausted'] == exhausted
        assert first['hits'] == 1 and empty['hits'] == 0
        check = doctor.check_fts()
        assert check['coverage'] == st['coverage']
        assert check['remedies'] == st['remedies']
    finally:
        src._conn.close()


def test_backfill_reports_no_identities_and_leaves_absent_index_absent(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [])
    idx.backfill()
    assert not fts.db_path().exists()
    idx.build()
    idx.backfill()
    st = idx.status()
    assert st['recovery']['state'] == 'no_identities'
    assert st['recovery']['last_attempt_at']
    assert st['recovery']['last_error'] is None
    assert 'no_identities' in _causes(st)
    assert 'provider_error' not in _causes(st)


def test_backfill_local_reasons_distinguish_no_lane_identifiers_and_misses(mail_fixture, monkeypatch):
    _add_ews_mailbox(mail_fixture)
    for rid in (510, 511, 512):
        _add_envelope_row_in(mail_fixture, rid, EWS_MBOX_ROWID)
    _write_ews_partial(mail_fixture, 510, None)
    _write_ews_partial(mail_fixture, 511, '<missing@cern.ch>')
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [])
    monkeypatch.setattr(FtsIndex, '_fetch_remote_body', lambda *a: None)
    idx.backfill()
    st = idx.status()
    assert st['recovery']['no_lane'] == 1
    assert st['recovery']['no_message_id'] == 1
    assert st['recovery']['no_remote_id'] == 1
    assert st['recovery']['confirmed_misses'] == 1
    assert st['recovery']['unclassified_unavailable'] == 0
    assert {'no_lane', 'missing_identifier', 'confirmed_miss'} <= _causes(st)
    before = st['recovery']
    idx.rebuild()
    assert idx.status()['recovery'] == before
    with sqlite3.connect(mail_fixture / 'MailData' / 'Envelope Index') as conn:
        conn.execute("UPDATE messages SET remote_id='remote512' WHERE ROWID=512")
    conn.close()
    monkeypatch.setattr(FtsIndex, '_translate_ews_ids', lambda *a: {})
    idx.backfill()
    st = idx.status()
    assert st['recovery']['no_remote_id'] == 0
    assert st['recovery']['confirmed_misses'] == 2


def test_imap_failure_reports_provider_error_without_false_miss_or_graph_login(mail_fixture, monkeypatch):
    _add_envelope_row_in(mail_fixture, 520, 2)
    _write_imap_partial(mail_fixture, 520, '<imap520@test>')
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [_ImapIdent()])

    def fail(*args):
        raise RuntimeError('IMAP authentication rejected')

    monkeypatch.setattr(FtsIndex, '_fetch_imap_body', fail)
    idx.backfill()
    st = idx.status()
    assert st['recovery']['state'] == 'error'
    assert 'IMAP authentication rejected' in st['recovery']['last_error']
    assert st['recovery']['confirmed_misses'] == 0
    assert 'provider_error' in _causes(st)
    check = doctor.check_fts()
    assert check['advisory'] is True
    assert 'graph' not in check['fix'].lower()
    assert 'login' not in check['fix'].lower()
    monkeypatch.setattr(FtsIndex, '_fetch_imap_body', lambda *a: {'content': 'recovered', 'contentType': 'text'})
    idx.backfill()
    assert idx.status()['recovery']['last_error'] is None
    assert 'provider_error' not in _causes(idx.status())


def test_unavailable_mailbox_join_is_deferred_not_no_lane(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    monkeypatch.setattr(FtsIndex, '_graph_identities', lambda self: [_Ident()])
    monkeypatch.setattr(FtsIndex, '_imap_identities', lambda self: [])
    original = idx._envelope_backfill_meta
    monkeypatch.setattr(idx, '_envelope_backfill_meta', lambda rows: {})
    assert idx.backfill()['deferred'] == 1
    recovery = idx.status()['recovery']
    assert recovery['no_lane'] == recovery['confirmed_misses'] == 0
    assert recovery['unavailable_mailbox'] == 1
    assert 'mailbox_metadata' in _causes(idx.status())
    monkeypatch.setattr(idx, '_envelope_backfill_meta', original)
    idx.backfill()
    assert idx.status()['recovery']['unavailable_mailbox'] == 0


@pytest.mark.parametrize('legacy', [1, 2])
def test_status_reads_legacy_schema_without_migrating_or_writing(mail_fixture, legacy):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    with sqlite3.connect(fts.db_path()) as conn:
        conn.execute('DROP TABLE absence')
        conn.execute('DROP TABLE backfill_reasons')
        conn.execute('UPDATE meta SET value=? WHERE key="schema_version"', (str(legacy),))
        if legacy == 1:
            conn.execute('DROP INDEX docs_source')
            conn.execute('ALTER TABLE docs DROP COLUMN source')
    conn.close()
    before = fts.db_path().read_bytes()
    st = idx.status()
    assert st['state'] == 'ready'
    assert st['schema_version'] == legacy
    assert st['coverage']['state'] == 'incomplete'
    assert st['recovery']['unclassified_unavailable'] == 0
    assert fts.db_path().read_bytes() == before
    with sqlite3.connect(fts.db_path()) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='absence'").fetchall()
    conn.close()
    idx.incremental()
    assert idx.status()['schema_version'] == 3


def test_coverage_never_claims_complete_when_backlog_unknown(mail_fixture, monkeypatch):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _write_body(mail_fixture, 200, 'now downloaded')
    _write('UPDATE docs SET last_attempt=0 WHERE rowid=200')
    idx.incremental()
    assert idx.status()['coverage']['state'] == 'no_known_gaps'

    def fail(*args):
        raise OSError('local store unavailable')

    monkeypatch.setattr(idx, '_envelope_backlog', fail)
    st = idx.status()
    assert st['backlog'] is None
    assert st['coverage']['state'] == 'unknown'
    assert 'backlog_unknown' in _causes(st)


@pytest.mark.parametrize('total,advisory', [(10, False), (1001, True)])
def test_doctor_small_and_large_gaps_have_same_guidance(monkeypatch, total, advisory):
    st = {'state': 'ready', 'backlog': 0, 'docs': {'total': total, 'indexed': 1, 'missing': total-1}}
    st.update(coverage_report(st))
    monkeypatch.setattr(fts, 'status', lambda: st)
    report = doctor.check_fts()
    assert report.get('advisory', False) is advisory
    assert report['coverage']['state'] == 'incomplete'
    assert 'body_gaps' in _causes(report)
    assert '--backfill' in report['fix']


def test_extraction_errors_have_distinct_search_count_and_guidance(mail_fixture):
    idx = FtsIndex(mail_base=mail_fixture)
    idx.build()
    _write("UPDATE docs SET status='error' WHERE rowid=200")
    src = AppleMailSource(mail_base=mail_fixture)
    try:
        report = src._fts_report(idx, [], False)
        assert report['errors'] == 1
        assert report['missing'] == 0
        assert 'error' not in report
        assert 'extraction_error' in _causes(report)
    finally:
        src._conn.close()
