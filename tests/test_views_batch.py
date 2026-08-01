"""Server-layer read shapes: get_email views (under the v0.11 {ok, email}
envelope), get_emails_batch, and the search_emails {ok, fts, results}
envelope with per-hit body_match.

Runs the real tool functions against the fake Mail fixture by pinning the
server's lazy source singleton to an AppleMailSource over mail_fixture.
"""
from __future__ import annotations

import json

import pytest

from email_mcp import server
from email_mcp.envelope import to_jsonable
from email_mcp.fts import FtsIndex
from email_mcp.server import (
    tool_get_email,
    tool_get_emails_batch,
    tool_list_recent,
    tool_list_scheduled,
    tool_search_emails,
)
from email_mcp.sources.apple_mail import AppleMailSource


@pytest.fixture
def src(mail_fixture, monkeypatch) -> AppleMailSource:
    s = AppleMailSource(mail_base=mail_fixture)
    monkeypatch.setattr(server, "_SOURCE", s)
    return s


# --------------------------------------------------------------------- #
# get_email views                                                       #
# --------------------------------------------------------------------- #


def test_view_full_data_is_byte_identical_under_the_envelope(src):
    """v0.11: the bare v0.7 dict took its one allowed break into {ok,
    email} (contract §1 row 2) — the email DATA itself is unchanged."""
    legacy = json.dumps(to_jsonable(src.get("100")), sort_keys=True)
    out = tool_get_email("100")
    assert out["ok"] is True and set(out) == {"ok", "email"}
    assert json.dumps(out["email"], sort_keys=True) == legacy
    full = tool_get_email("100", view="full")
    assert json.dumps(full["email"], sort_keys=True) == legacy


def test_view_metadata_drops_bodies_keeps_the_rest(src):
    out = tool_get_email("101", view="metadata")["email"]
    assert "body_text" not in out
    assert "body_html" not in out
    assert out["ref"]["id"] == "101"
    assert out["headers"]["Subject"] == "EMCI production update"
    assert [a["name"] for a in out["attachments"]] == ["production.csv"]
    assert out["flags"] == {"read": True, "flagged": False}


def test_view_minimal_is_the_skeleton(src):
    out = tool_get_email("100", view="minimal")["email"]
    assert set(out) == {"id", "subject", "from_addr", "date", "mailbox",
                        "unread"}
    assert out["id"] == "100"
    assert out["subject"] == "I2C disclosure on April 20"
    assert out["from_addr"] == "Stefan Schlenker <stefan.schlenker@cern.ch>"
    assert out["mailbox"] == "Inbox"
    assert out["unread"] is True


def test_invalid_view_rejected_with_code(src):
    out = tool_get_email("100", view="everything")
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert "view" in out["error"]
    assert "fix" not in out  # designed reject, not a belt catch
    out = tool_get_emails_batch(["100"], view="everything")
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert "view" in out["error"]


# --------------------------------------------------------------------- #
# get_emails_batch                                                      #
# --------------------------------------------------------------------- #


def test_batch_happy_two_hits_one_error_as_data(src):
    out = tool_get_emails_batch(["100", "101", "999"])
    assert out["ok"] is True
    assert out["view"] == "full"
    assert [e["ref"]["id"] for e in out["emails"]] == ["100", "101"]
    assert "retracted" in out["emails"][0]["body_text"]
    assert len(out["errors"]) == 1
    assert out["errors"][0]["id"] == "999"
    assert "not found" in out["errors"][0]["error"]
    assert out["errors"][0]["code"] == "not_found"  # §3.2: errors[].code


def test_batch_respects_view(src):
    out = tool_get_emails_batch(["100", "101"], view="minimal")
    assert out["view"] == "minimal"
    assert all(set(e) == {"id", "subject", "from_addr", "date", "mailbox",
                          "unread"} for e in out["emails"])


def test_batch_over_cap_rejected_outright(src):
    out = tool_get_emails_batch([str(i) for i in range(51)])
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert "50" in out["error"]
    assert "emails" not in out  # rejected, not partially served


def test_batch_of_huge_bad_ids_cannot_flood_the_wire(src):
    """50 ids of 60 KB each, AT the cap: every echo in errors[] is a
    failure record the boundary byte-bounds — the envelope stays small."""
    out = tool_get_emails_batch(["Z" * 60000] * 50)
    assert out["ok"] is True and len(out["errors"]) == 50
    assert len(json.dumps(out).encode("utf-8")) < 250_000


# --------------------------------------------------------------------- #
# page cap — search / list_recent / list_scheduled (§5)                  #
# --------------------------------------------------------------------- #
# Caps reject, never truncate — and the page cap is the server's own
# survival: `limit` had no ceiling, so a 20,000-row page pulled the whole
# corpus into one envelope and took the server down on the first real
# user's machine (2026-08-01).


@pytest.mark.parametrize("call", [
    lambda: tool_search_emails(limit=501),
    lambda: tool_search_emails(limit=20000),
    lambda: tool_search_emails(limit=0),
    lambda: tool_search_emails(limit=-5),
    lambda: tool_list_recent(limit=501),
    lambda: tool_list_recent(limit=0),
    lambda: tool_list_scheduled(limit=501),
    # `[-limit:]` with limit=0 is `[0:]` — the WHOLE spool, the opposite
    # of "give me nothing"; the gate closes the slice hole too.
    lambda: tool_list_scheduled(limit=0),
])
def test_over_cap_pages_reject_outright(src, call):
    out = call()
    assert out["ok"] is False and out["code"] == "invalid_input"
    assert "500" in out["error"]


def test_at_cap_page_passes(src):
    assert tool_search_emails(limit=500)["ok"] is True
    assert tool_list_recent(limit=500)["ok"] is True


# --------------------------------------------------------------------- #
# search_emails envelope + body_match                                   #
# --------------------------------------------------------------------- #


def test_search_envelope_body_match_annotation(src, mail_fixture):
    FtsIndex(mail_base=mail_fixture).build()

    out = tool_search_emails(query="retracted")
    assert out["ok"] is True
    fts = out["fts"]
    assert fts["state"] == "ready"
    assert fts["hits"] == 1
    assert fts["hits_capped"] is False
    assert "rowids" not in fts  # internal channel never leaks
    assert [r["id"] for r in out["results"]] == ["100"]
    assert out["results"][0]["body_match"] is True

    # "I2C" also hits message 100's body via FTS, but the query is already
    # visible in the subject — so it is NOT annotated as a body match.
    out = tool_search_emails(query="I2C")
    r100 = next(r for r in out["results"] if r["id"] == "100")
    assert r100["body_match"] is False


def test_search_envelope_present_without_query(src):
    out = tool_search_emails(unread_only=True)
    assert out["ok"] is True
    assert out["fts"]["hits"] == 0
    assert out["fts"]["state"] == "absent"  # guard dir holds no index
    assert [r["id"] for r in out["results"]] == ["100"]
    assert out["results"][0]["body_match"] is False


# --------------------------------------------------------------------- #
# the formerly-bare tools (contract §1 rows 4-7): their one allowed     #
# break into envelopes, taken at v0.11                                  #
# --------------------------------------------------------------------- #


def test_bare_array_tools_gained_envelopes(src):
    out = server.tool_get_thread("7001")
    assert out["ok"] is True and set(out) == {"ok", "thread"}
    assert [r["id"] for r in out["thread"]] == ["200", "100"]  # oldest first

    out = server.tool_list_mailboxes()
    assert out["ok"] is True and set(out) == {"ok", "mailboxes"}
    assert {m["name"] for m in out["mailboxes"]} == {"Inbox", "Promo",
                                                     "[Gmail]/All Mail"}

    out = server.tool_list_recent(limit=2)
    assert out["ok"] is True and set(out) == {"ok", "messages", "note"}
    assert [r["id"] for r in out["messages"]] == ["101", "100"]


def test_get_attachment_gained_the_envelope(src):
    out = server.tool_get_attachment("101", "2")
    assert out["ok"] is True and set(out) == {"ok", "attachment"}
    blob = out["attachment"]
    assert blob["name"] == "production.csv"
    assert set(blob) == {"name", "mime", "size", "path"}


# --------------------------------------------------------------------- #
# ghost mailboxes (field evidence 2026-08-01): Gmail label mailboxes    #
# advertise server-side counts with ZERO local rows — list_mailboxes    #
# reports both counts, and an empty page scoped to one says why.        #
# --------------------------------------------------------------------- #

IMAP_ACCT = "BBBBBBBB-0000-0000-0000-000000000002"
LOCAL_ACCT = "AAAAAAAA-0000-0000-0000-000000000001"


def test_list_mailboxes_carries_local_count_alongside_total(src):
    boxes = {m["name"]: m for m in server.tool_list_mailboxes()["mailboxes"]}
    assert boxes["Inbox"]["total"] == 3
    assert boxes["Inbox"]["local_count"] == 3
    # The ghost stays advertised — it IS real server-side (hiding it
    # would lie in the other direction) — but both numbers are on show.
    assert boxes["Promo"]["total"] == 133
    assert boxes["Promo"]["local_count"] == 0


def test_scoped_search_on_a_ghost_mailbox_says_why_it_is_empty(src):
    out = tool_search_emails(mailbox="Promo")
    assert out["ok"] is True and out["results"] == []
    assert "133" in out["note"]
    assert "[Gmail]/All Mail" in out["note"]


def test_scoped_list_recent_on_a_ghost_mailbox_says_why_it_is_empty(src):
    out = tool_list_recent(mailbox="Promo")
    assert out["ok"] is True and out["messages"] == []
    assert "[Gmail]/All Mail" in out["note"]


def test_note_respects_the_account_scope(src):
    # Scoped to an account that has no such mailbox: nothing matched the
    # scope at all — not the ghost situation, no note.
    out = tool_search_emails(mailbox="Promo", account=LOCAL_ACCT)
    assert out["results"] == [] and out["note"] is None
    out = tool_search_emails(mailbox="Promo", account=IMAP_ACCT)
    assert out["note"] is not None


def test_normal_and_unknown_mailboxes_carry_no_note(src):
    # Populated scope with hits: no note.
    out = tool_search_emails(mailbox="Inbox")
    assert out["results"] and out["note"] is None
    # Populated scope emptied by filters: real emptiness, no note.
    out = tool_search_emails(mailbox="Inbox", query="zzz-no-such-thing")
    assert out["results"] == [] and out["note"] is None
    # Unknown mailbox: the scope matched nothing — no note.
    out = tool_search_emails(mailbox="NoSuchBox")
    assert out["results"] == [] and out["note"] is None
    # Unscoped empty search: no note.
    out = tool_search_emails(query="zzz-no-such-thing")
    assert out["results"] == [] and out["note"] is None
    # Populated scoped list_recent: no note.
    out = tool_list_recent(mailbox="Inbox")
    assert out["messages"] and out["note"] is None
