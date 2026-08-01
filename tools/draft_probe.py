#!/usr/bin/env python3
"""Apple Mail draft-fidelity probe — Lane A of docs/draft-design.md.

Answers ONE question against the real Mail.app: does AppleScript's
scripted-compose path (`make new outgoing message … save`) store a
faithful multi-paragraph draft, or does it inherit the collapsed-
blockquote corruption this project was founded on (so far measured only
on send, never on save)?  Safe by construction: the rendered script
composes and saves — it contains no send verb to reach — and the probe
refuses a verdict unless the draft is verified in a Drafts mailbox
through the Envelope Index (read-only, as always).

The machine half ends at dumping the STORED bytes for inspection; the
human half — send the draft by hand from Mail.app to an Outlook
recipient, record the verdict in docs/draft-design.md — is printed as
next steps.  USER-RUN, like tools/graph_probe.py; never executed by the
test suite.
"""

import argparse
import email
import email.policy
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from email_mcp.config import mail_dir  # noqa: E402
from email_mcp.sources.apple_mail import (  # noqa: E402
    _connect_readonly,
    _read_emlx_bytes,
)
from email_mcp.sources.apple_mail_paths import (  # noqa: E402
    account_label,
    build_emlx_path,
    mailbox_name,
)

# Three paragraphs: prose, blank-line separation, non-ASCII — exactly
# what the founding blockquote bug destroyed on the send path.
PARAGRAPHS = [
    "Probe paragraph ONE: plain prose on a single line.",
    "Probe paragraph TWO arrives after a blank line — paragraph breaks "
    "are what the blockquote bug collapsed.",
    "Probe paragraph THREE carries non-ASCII (éü — Genève) and closes "
    "the body.",
]
MARKERS = ["paragraph ONE", "paragraph TWO", "paragraph THREE"]

_WAIT = 30.0  # seconds to wait for the index row / the .emlx on disk
_POLL = 1.0


class ProbeError(Exception):
    """Fatal probe failure; str(exc) becomes the verdict reason."""


def info(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------- #
# pure helpers (unit-tested in tests/test_draft_probe.py)               #
# --------------------------------------------------------------------- #


def as_literal(s: str) -> str:
    """Quote one LINE for AppleScript source (newlines are handled by
    body_expression; any other control character refuses)."""
    if any(ord(c) < 0x20 for c in s):
        raise ProbeError(f"control character in {s!r} — cannot script it")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def body_expression(text: str) -> str:
    """Multi-line text as an AppleScript expression — literals cannot
    span lines, so line breaks become `& linefeed &` joins."""
    return " & linefeed & ".join(as_literal(ln) for ln in text.split("\n"))


def render_script(subject: str, to_addr: str, body: str) -> str:
    """The whole AppleScript the probe runs: compose + save, nothing
    else — by construction there is no send verb to reach."""
    return (
        'tell application "Mail"\n'
        "    set draftMsg to make new outgoing message with properties "
        f"{{visible:false, subject:{as_literal(subject)}, "
        f"content:{body_expression(body)}}}\n"
        "    tell draftMsg to make new to recipient at end of to recipients "
        f"with properties {{address:{as_literal(to_addr)}}}\n"
        "    save draftMsg\n"
        "end tell\n"
    )


def inspect_stored(raw: bytes, markers: list[str]) -> dict:
    """Fidelity summary of stored RFC-822 bytes: which text parts exist,
    which probe markers each kept, and whether the founding bug's
    <blockquote> wrapper appears."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    parts: dict[str, dict] = {}
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart() or ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        parts[ctype] = {
            "markers_kept": [m for m in markers if m in text],
            "blockquote_wrapper": "<blockquote" in text.lower(),
        }
    return {"content_type": msg.get_content_type(), "parts": parts}


# --------------------------------------------------------------------- #
# the probe                                                             #
# --------------------------------------------------------------------- #


def _find_row(index: Path, subject: str) -> tuple[int, str] | None:
    """(rowid, mailbox_url) for the probe subject. Fresh read-only
    connection per poll — the Envelope Index is never opened writable."""
    conn = _connect_readonly(index)
    try:
        row = conn.execute(
            "SELECT m.ROWID AS rowid, mb.url AS url"
            "  FROM messages m"
            "  JOIN subjects s ON s.ROWID = m.subject"
            "  JOIN mailboxes mb ON mb.ROWID = m.mailbox"
            " WHERE s.subject = ?",
            (subject,),
        ).fetchone()
    finally:
        conn.close()
    return (int(row["rowid"]), row["url"] or "") if row else None


def _probe(args, verdict):
    base = mail_dir()
    index = base / "MailData" / "Envelope Index"
    if not index.exists():
        raise ProbeError(f"Envelope Index not found at {index}")

    script = render_script(verdict["subject"], args.to,
                           "\n\n".join(PARAGRAPHS))
    try:
        proc = subprocess.run(["osascript", "-"], input=script,
                              capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ProbeError("osascript not found — this probe is macOS-only")
    except subprocess.TimeoutExpired:
        raise ProbeError("Mail.app did not answer the compose script in 60s")
    if proc.returncode != 0:
        raise ProbeError("compose script failed: "
                         + (proc.stderr or "").strip())
    verdict["composed"] = True

    deadline = time.time() + _WAIT
    found = None
    while found is None and time.time() < deadline:
        found = _find_row(index, verdict["subject"])
        if found is None:
            time.sleep(_POLL)
    if found is None:
        raise ProbeError(f"draft not in the Envelope Index within "
                         f"{_WAIT:.0f}s — cannot verify it landed in Drafts")
    rowid, url = found
    verdict["rowid"] = rowid
    verdict["mailbox"] = mailbox_name(url)
    verdict["account"] = account_label(url)
    if verdict["mailbox"].split("/")[-1] != "Drafts":
        raise ProbeError(f"draft landed in {verdict['mailbox']!r}, "
                         "not a Drafts mailbox")
    verdict["in_drafts"] = True

    raw = b""
    deadline = time.time() + _WAIT
    while not raw and time.time() < deadline:
        try:
            path = build_emlx_path(base, url, rowid)
        except FileNotFoundError:
            path = None
        if path is not None and path.exists():
            raw = _read_emlx_bytes(path)
        else:
            time.sleep(_POLL)
    if not raw:
        raise ProbeError("draft row exists but its .emlx never appeared "
                         "on disk — stored bytes unavailable")

    out = Path(f"draft-probe-{rowid}.eml")
    out.write_bytes(raw)
    verdict["stored_dump"] = str(out.resolve())
    verdict["structure"] = inspect_stored(raw, MARKERS)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="draft_probe.py",
        description="Create ONE multi-paragraph draft in Mail.app via "
                    "AppleScript (saved to Drafts, NEVER sent), verify it "
                    "landed in Drafts via the Envelope Index, and dump its "
                    "stored bytes for fidelity inspection. Prints a JSON "
                    "verdict; exit 0 = verified + dumped, 1 = refused.")
    parser.add_argument("--to", required=True,
                        help="recipient address for the draft — the Outlook "
                             "mailbox you will later send it to BY HAND")
    parser.add_argument("--json", action="store_true",
                        help="compact machine-readable verdict on stdout")
    args = parser.parse_args(argv)

    subject = ("email-mcp draft probe "
               + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    verdict = {"probe": "NO", "subject": subject, "composed": False,
               "rowid": None, "mailbox": "", "account": "",
               "in_drafts": False, "stored_dump": "", "structure": {},
               "reason": ""}
    try:
        _probe(args, verdict)
    except ProbeError as e:
        verdict["reason"] = str(e)
    if verdict["in_drafts"] and verdict["stored_dump"]:
        verdict["probe"] = "YES"
        info("Draft verified in Drafts (account %s); stored bytes: %s"
             % (verdict["account"], verdict["stored_dump"]))
        info("Manual half of the Lane A verdict:")
        info("  1. Inspect the dump: all three paragraphs in the plain AND "
             "html parts? any <blockquote> wrapper?")
        info("  2. In Mail.app, open the draft from Drafts and send it BY "
             "HAND to the Outlook recipient.")
        info("  3. Check the rendering in Outlook (body present, paragraphs "
             "intact).")
        info("  4. Record the verdict in docs/draft-design.md.")
    elif verdict["composed"]:
        info("note: the compose script ran — a probe draft may exist in "
             "Mail's Drafts even though verification failed; check by hand.")
    print(json.dumps(verdict) if args.json else json.dumps(verdict, indent=2))
    return 0 if verdict["probe"] == "YES" else 1


if __name__ == "__main__":
    sys.exit(main())
