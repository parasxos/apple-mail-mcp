"""Server-layer read shapes: get_email views, get_emails_batch, and the
search_emails {ok, fts, results} envelope with per-hit body_match.

Runs the real tool functions against the fake Mail fixture by pinning the
server's lazy source singleton to an AppleMailSource over mail_fixture.
"""
from __future__ import annotations

import json

import pytest

from email_mcp import server
from email_mcp.fts import FtsIndex
from email_mcp.server import (
    _to_jsonable,
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


def test_view_full_is_byte_identical_to_v071_shape(src):
    legacy = json.dumps(_to_jsonable(src.get("100")), sort_keys=True)
    assert json.dumps(tool_get_email("100"), sort_keys=True) == legacy
    assert json.dumps(tool_get_email("100", view="full"), sort_keys=True) == legacy


def test_view_metadata_drops_bodies_keeps_the_rest(src):
    out = tool_get_email("101", view="metadata")
    assert "body_text" not in out
    assert "body_html" not in out
    assert out["ref"]["id"] == "101"
    assert out["headers"]["Subject"] == "EMCI production update"
    assert [a["name"] for a in out["attachments"]] == ["production.csv"]
    assert out["flags"] == {"read": True, "flagged": False}


def test_view_minimal_is_the_skeleton(src):
    out = tool_get_email("100", view="minimal")
    assert set(out) == {"id", "subject", "from_addr", "date", "mailbox",
                        "unread"}
    assert out["id"] == "100"
    assert out["subject"] == "I2C disclosure on April 20"
    assert out["from_addr"] == "Stefan Schlenker <stefan.schlenker@cern.ch>"
    assert out["mailbox"] == "Inbox"
    assert out["unread"] is True


def test_invalid_view_rejected_as_data(src):
    out = tool_get_email("100", view="everything")
    assert out["ok"] is False
    assert "view" in out["error"]
    out = tool_get_emails_batch(["100"], view="everything")
    assert out["ok"] is False
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


def test_batch_respects_view(src):
    out = tool_get_emails_batch(["100", "101"], view="minimal")
    assert out["view"] == "minimal"
    assert all(set(e) == {"id", "subject", "from_addr", "date", "mailbox",
                          "unread"} for e in out["emails"])


def test_batch_over_cap_rejected_outright(src):
    out = tool_get_emails_batch([str(i) for i in range(51)])
    assert out["ok"] is False
    assert "50" in out["error"]
    assert "emails" not in out  # rejected, not partially served


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
