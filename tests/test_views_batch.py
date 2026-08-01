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
    assert {m["name"] for m in out["mailboxes"]} == {"Inbox",
                                                     "[Gmail]/All Mail"}

    out = server.tool_list_recent(limit=2)
    assert out["ok"] is True and set(out) == {"ok", "messages"}
    assert [r["id"] for r in out["messages"]] == ["101", "100"]


def test_get_attachment_gained_the_envelope(src):
    out = server.tool_get_attachment("101", "2")
    assert out["ok"] is True and set(out) == {"ok", "attachment"}
    blob = out["attachment"]
    assert blob["name"] == "production.csv"
    assert set(blob) == {"name", "mime", "size", "path"}
