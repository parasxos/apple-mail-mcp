"""Pure-function tests for tools/draft_probe.py — no osascript, no Mail."""
from __future__ import annotations

import importlib.util
from email.message import EmailMessage
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parent.parent / "tools" / "draft_probe.py"
    spec = importlib.util.spec_from_file_location("draft_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dp = _load()


# ---------------------------------------------------------------- #
# composition                                                      #
# ---------------------------------------------------------------- #


def test_as_literal_escapes_quotes_and_backslashes():
    assert dp.as_literal('a"b\\c') == '"a\\"b\\\\c"'


def test_as_literal_refuses_control_characters():
    with pytest.raises(dp.ProbeError):
        dp.as_literal("tab\there")


def test_body_expression_joins_lines_with_linefeed():
    assert (dp.body_expression("p1\n\np2")
            == '"p1" & linefeed & "" & linefeed & "p2"')


def test_render_script_composes_and_saves_never_sends():
    script = dp.render_script("probe subject", "someone@example.com",
                              "\n\n".join(dp.PARAGRAPHS))
    assert "make new outgoing message" in script
    assert "save draftMsg" in script
    assert '"probe subject"' in script
    assert '"someone@example.com"' in script
    # The invariant, lexically: no send verb anywhere in the script.
    assert "send" not in script.lower()


def test_render_script_escapes_subject():
    script = dp.render_script('a "quoted" subject', "a@b.c", "body")
    assert '"a \\"quoted\\" subject"' in script


# ---------------------------------------------------------------- #
# stored-bytes inspection                                          #
# ---------------------------------------------------------------- #


def _alternative(plain: str, html: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "probe"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return bytes(msg)


def test_inspect_stored_reports_parts_markers_and_blockquote():
    raw = _alternative(
        "paragraph ONE\n\nparagraph TWO\n\nparagraph THREE",
        '<html><body><blockquote type="cite"><div>paragraph ONE</div>'
        "<div>paragraph TWO</div></blockquote></body></html>",
    )
    got = dp.inspect_stored(raw, dp.MARKERS)
    assert got["content_type"] == "multipart/alternative"
    plain = got["parts"]["text/plain"]
    assert plain["markers_kept"] == dp.MARKERS
    assert plain["blockquote_wrapper"] is False
    html = got["parts"]["text/html"]
    assert html["markers_kept"] == ["paragraph ONE", "paragraph TWO"]
    assert html["blockquote_wrapper"] is True


def test_inspect_stored_plain_only_message():
    msg = EmailMessage()
    msg["Subject"] = "probe"
    msg.set_content("paragraph ONE only — éü survives")
    got = dp.inspect_stored(bytes(msg), dp.MARKERS)
    assert got["content_type"] == "text/plain"
    assert got["parts"]["text/plain"]["markers_kept"] == ["paragraph ONE"]
    assert "text/html" not in got["parts"]
