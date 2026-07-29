"""Triage: mailbox management as selection × disposition.

Pipeline (see docs/triage-design.md — every number below was measured on
the live store): SELECT via the existing SQLite read layer → PLAN frozen
to disk → ACT in ONE batched AppleScript addressing messages as
`«class mssg» id <ROWID>` (0.16 s keyed lookup vs 85.6 s for a whose-scan
in a 72k mailbox — Mail's AppleScript object id IS the Envelope Index
ROWID) → VERIFY by re-reading the index (write-through ≤2 s).

The Envelope Index is never opened writable; all mutations go through
Mail.app itself, which owns server sync (EWS/IMAP alike).
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta

from . import config, plans
from .log import get_logger
from .plans import Plan, PlanAction, PlanMessage
from .sources.base import SearchQuery

_log = get_logger()

RELOCATING = {"move_to", "delete"}
# The destructive verb is segregated: `delete` is NOT in the shared actions
# vocabulary — it enters only through build_delete_plan (triage_plan_delete),
# so a fumbled parameter on triage_plan can never reach the Trash.
ACTIONS = {"move_to", "mark_read", "mark_unread", "flag", "unflag"}
DESTRUCTIVE = {"delete"}


class TriageError(Exception):
    """Caller-fixable triage failure with a machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# --------------------------------------------------------------------- #
# plan side                                                             #
# --------------------------------------------------------------------- #


def _parse_actions(raw: list[dict] | None,
                   allowed: set[str] = ACTIONS) -> list[PlanAction]:
    if not raw:
        raise TriageError("invalid_action", "`actions` is required (non-empty list).")
    parsed: list[PlanAction] = []
    for item in raw:
        if not isinstance(item, dict) or "action" not in item:
            raise TriageError("invalid_action", f"malformed action entry: {item!r}")
        verb = str(item["action"])
        if verb not in allowed:
            if verb in DESTRUCTIVE:
                raise TriageError(
                    "destructive_action",
                    "delete has its own tool — use triage_plan_delete.",
                )
            raise TriageError(
                "invalid_action",
                f"unknown action {verb!r} (want one of {sorted(allowed)})",
            )
        mailbox = item.get("mailbox")
        color = item.get("color")
        if verb == "move_to" and not mailbox:
            raise TriageError("invalid_action", "move_to needs `mailbox`.")
        if verb == "flag":
            color = 0 if color is None else int(color)
            if not 0 <= color <= 6:
                raise TriageError("invalid_action", f"flag color {color} not in 0..6.")
        parsed.append(PlanAction(action=verb, mailbox=mailbox, color=color))

    verbs = [a.action for a in parsed]
    if len(verbs) != len(set(verbs)):
        raise TriageError("conflicting_actions", "duplicate actions in one plan.")
    conflicts = [
        {"mark_read", "mark_unread"},
        {"flag", "unflag"},
        {"move_to", "delete"},
    ]
    for pair in conflicts:
        if pair <= set(verbs):
            raise TriageError(
                "conflicting_actions", f"{' + '.join(sorted(pair))} conflict."
            )
    # A relocating action kills the by-ROWID specifier — always run it last.
    parsed.sort(key=lambda a: a.action in RELOCATING)
    return parsed


def _summary(messages: list[PlanMessage], actions: list[PlanAction],
             target: dict | None) -> str:
    verbs = " + ".join(
        a.action + (f" '{a.mailbox}'" if a.action == "move_to" else "")
        for a in actions
    )
    senders = len({m.from_addr for m in messages})
    dates = sorted(m.date[:10] for m in messages)
    accounts = {m.account for m in messages}
    return (
        f"{verbs}: {len(messages)} msg(s), {senders} sender(s), "
        f"{dates[0]}…{dates[-1]}, account(s) {', '.join(sorted(accounts))}"
    )


def _scheme(url: str) -> str:
    return url.split("://", 1)[0] if "://" in url else ""


def build_plan(source, q: SearchQuery, actions: list[dict] | None,
               allowed: set[str] = ACTIONS) -> Plan:
    plans.gc()
    parsed = _parse_actions(actions, allowed=allowed)

    snap_fn = getattr(source, "triage_snapshot", None)
    resolve_fn = getattr(source, "resolve_mailbox", None)
    if snap_fn is None or resolve_fn is None:
        raise TriageError("unsupported_source",
                          "this email source does not support triage.")

    cap = config.triage_max_messages()
    refs = source.search(q)
    if not refs:
        raise TriageError("empty_selection", "no messages match the query.")
    if len(refs) > cap:
        raise TriageError(
            "selection_too_large",
            f"query matched more than {cap} messages (the cap; counting "
            "stopped there) — narrow the query, or raise EMAIL_MCP_TRIAGE_MAX "
            "if the selection is genuinely intended.",
        )

    rowids = [int(r.id) for r in refs]
    snap = snap_fn(rowids)

    target: dict | None = None
    move = next((a for a in parsed if a.action == "move_to"), None)
    if move is not None:
        accounts = {r.account for r in refs}
        if len(accounts) > 1:
            raise TriageError(
                "cross_account",
                f"selection spans {len(accounts)} accounts "
                f"({', '.join(sorted(accounts))}); move_to needs one — "
                "add the account= filter to pick which.",
            )
        account = next(iter(accounts))
        hit = resolve_fn(account, move.mailbox)
        if hit is not None:
            target = {"account": account, "mailbox": move.mailbox,
                      "mailbox_rowid": hit[0], "url": hit[1]}
            if all(snap.get(rid, {}).get("mailbox_rowid") == hit[0]
                   for rid in rowids):
                raise TriageError(
                    "noop_move",
                    "every selected message is already in that mailbox.")
        else:
            # Not in the Envelope Index — a freshly created, never-synced
            # mailbox has no index row yet (observed live). Ask Mail itself;
            # if it exists, plan with an unknown target rowid and verify the
            # move as "message left the source mailbox".
            scheme = _scheme(snap[rowids[0]]["mailbox_url"])
            if not _mailbox_exists_in_mail(scheme, account, move.mailbox):
                raise TriageError(
                    "unknown_mailbox",
                    f"mailbox {move.mailbox!r} not found in account {account} "
                    "(neither in the index nor in Mail.app).",
                )
            target = {"account": account, "mailbox": move.mailbox,
                      "mailbox_rowid": None, "url": f"{scheme}://{account}/?unsynced"}

    messages: list[PlanMessage] = []
    for ref in refs:
        rid = int(ref.id)
        s = snap.get(rid)
        if s is None:
            continue  # vanished between search and snapshot; plan what exists
        _as_literal(ref.mailbox)   # fail early on unrepresentable names
        messages.append(PlanMessage(
            rowid=rid,
            account=ref.account,
            scheme=_scheme(s["mailbox_url"]),
            mailbox=ref.mailbox,
            mailbox_rowid=s["mailbox_rowid"],
            subject=ref.subject,
            from_addr=ref.from_addr,
            date=ref.date.isoformat(),
            unread=ref.unread,
            message_id_header=s["mid_header"],
            global_message_id=s["gmid"],
            pre={"read": s["read"], "flagged": s["flagged"],
                 "flag_color": s["flag_color"]},
        ))
    if not messages:
        raise TriageError("empty_selection",
                          "all matched messages vanished before planning.")

    now = plans.utcnow()
    plan = Plan(
        id=plans.new_id(now),
        created_at=plans.iso(now),
        expires_at=plans.iso(now + timedelta(seconds=config.triage_ttl_seconds())),
        status="draft",
        query={k: v for k, v in vars(q).items() if v not in (None, "", False, 0)},
        actions=parsed,
        target=target,
        messages=messages,
        summary=_summary(messages, parsed, target),
    )
    plans.save(plan)
    _log.info("triage plan %s: %s", plan.id, plan.summary)
    return plan


def delete_max() -> int:
    """Cap for delete plans (tighter than triage_max_messages — the verb is
    destructive)."""
    return config.triage_delete_max()


def build_delete_plan(source, q: SearchQuery) -> Plan:
    """Stage deletion — the destructive verb's own door (triage_plan_delete).

    Two guards run before the ordinary planning path: the selection must
    live in ONE account (symmetric with move_to's guard) and must fit the
    tighter delete cap. The result is a normal frozen Plan carrying the one
    `delete` action — it expires, is reviewed and applies through the
    unchanged apply_plan exactly like any other plan."""
    refs = source.search(q)
    accounts = {r.account for r in refs}
    if len(accounts) > 1:
        raise TriageError(
            "cross_account",
            f"selection spans {len(accounts)} accounts "
            f"({', '.join(sorted(accounts))}); delete needs one — "
            "add the account= filter to pick which.",
        )
    cap = delete_max()
    if len(refs) > cap:
        raise TriageError(
            "selection_too_large",
            f"query matched more than {cap} messages (the delete cap) — "
            "narrow the query, or raise EMAIL_MCP_TRIAGE_DELETE_MAX if the "
            "deletion is genuinely intended.",
        )
    return build_plan(source, q, [{"action": "delete"}],
                      allowed=ACTIONS | DESTRUCTIVE)


# --------------------------------------------------------------------- #
# AppleScript generation                                                #
# --------------------------------------------------------------------- #


def _as_literal(s: str) -> str:
    """Quote a string for embedding in AppleScript source. Only mailbox
    names, account UUIDs and message-id headers ever pass through here."""
    if any(ord(c) < 0x20 for c in s):
        raise TriageError("invalid_name",
                          f"control character in name {s!r} — cannot script it.")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mailbox_exists_in_mail(scheme: str, account: str, name: str) -> bool:
    """Plan-time existence probe straight at Mail.app, for mailboxes the
    Envelope Index hasn't synced yet. Best-effort: errors read as absent."""
    spec = _mailbox_specifier(scheme, account, name)
    script = (
        'tell application "Mail"\n'
        f"    if exists {spec} then\n"
        '        return "YES"\n'
        "    end if\n"
        '    return "NO"\n'
        "end tell\n"
    )
    try:
        proc = _run_osascript(script, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "YES"


def _mailbox_specifier(scheme: str, account: str, name: str) -> str:
    """`local://` accounts have no AppleScript account object (verified
    live) — their mailboxes are application-level."""
    if scheme == "local":
        return f"mailbox {_as_literal(name)}"
    return f"mailbox {_as_literal(name)} of account id {_as_literal(account)}"


def _action_lines(a: PlanAction, target: dict | None,
                  scheme: str, account: str) -> list[str]:
    if a.action == "mark_read":
        return ["set read status of msgRef to true"]
    if a.action == "mark_unread":
        return ["set read status of msgRef to false"]
    if a.action == "flag":
        return [f"set flag index of msgRef to {a.color}"]
    if a.action == "unflag":
        return ["set flag index of msgRef to -1"]
    if a.action == "delete":
        return ["delete msgRef"]
    if a.action == "move_to":
        spec = _mailbox_specifier(_scheme(target["url"]), target["account"],
                                  target["mailbox"])
        return [f"move msgRef to {spec}"]
    raise TriageError("invalid_action", f"unrenderable action {a.action!r}")


def _render_preflight() -> str:
    return (
        'tell application "Mail"\n'
        "    set accountIds to {}\n"
        "    repeat with a in accounts\n"
        "        set end of accountIds to (id of a as text)\n"
        "    end repeat\n"
        "    set AppleScript's text item delimiters to linefeed\n"
        "    return accountIds as text\n"
        "end tell\n"
    )


def _render_script(plan: Plan) -> str:
    blocks: list[str] = []
    for m in plan.messages:
        spec = (f"«class mssg» id {m.rowid} of "
                f"{_mailbox_specifier(m.scheme, m.account, m.mailbox)}")
        acts = []
        for a in plan.actions:
            acts += _action_lines(a, plan.target, m.scheme, m.account)
        act_src = "\n".join(f"                {line}" for line in acts)
        if m.message_id_header:
            guard_open = (
                f"            set theMid to message id of msgRef\n"
                f"            if theMid does not contain "
                f"{_as_literal(m.message_id_header)} then\n"
                f'                set end of out to "ERR {m.rowid} mid_mismatch " & theMid\n'
                f"            else\n"
            )
            guard_close = (
                f'                set end of out to "OK {m.rowid}"\n'
                f"            end if\n"
            )
        else:  # 18-in-305k rows without a header: act without the recheck
            guard_open = ""
            guard_close = f'            set end of out to "OK {m.rowid}"\n'
            act_src = "\n".join(f"            {line}" for line in acts)
        blocks.append(
            f"        try\n"
            f"            set msgRef to {spec}\n"
            f"{guard_open}"
            f"{act_src}\n"
            f"{guard_close}"
            f"        on error eMsg number eNum\n"
            f'            set end of out to "ERR {m.rowid} applescript " '
            f'& (eNum as text) & " " & eMsg\n'
            f"        end try"
        )
    body = "\n".join(blocks)
    return (
        "on run\n"
        "    set out to {}\n"
        '    tell application "Mail"\n'
        f"{body}\n"
        "    end tell\n"
        "    set AppleScript's text item delimiters to linefeed\n"
        "    return out as text\n"
        "end run\n"
    )


# --------------------------------------------------------------------- #
# act + verify                                                          #
# --------------------------------------------------------------------- #


def _run_osascript(script: str, timeout: float) -> subprocess.CompletedProcess:
    """THE seam: tests monkeypatch this one symbol. Script goes on stdin
    (no ARG_MAX ceiling, no temp-file lifecycle)."""
    return subprocess.run(
        ["osascript", "-"],
        input=script, capture_output=True, text=True, timeout=timeout,
    )


def _osa_error_code(stderr: str) -> int | None:
    stderr = (stderr or "").strip()
    if "(-" in stderr and stderr.endswith(")"):
        try:
            return int(stderr.rsplit("(", 1)[1].rstrip(")"))
        except ValueError:
            return None
    return None


def _auto_timeout(n: int) -> float:
    override = config.triage_timeout_seconds()
    if override > 0:
        return override
    return max(60.0, min(300.0, 30.0 + 0.6 * n))


def _parse_batch_output(stdout: str, planned: list[int]) -> dict[int, tuple[str, str]]:
    """rowid -> ("ok","") | (code, detail). Ids the script never reported
    become ("no_result", ...)."""
    results: dict[int, tuple[str, str]] = {}
    for line in (stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0] in ("OK", "ERR"):
            try:
                rid = int(parts[1])
            except ValueError:
                continue
            if parts[0] == "OK":
                results[rid] = ("ok", "")
            else:
                detail = parts[2] if len(parts) > 2 else ""
                code = detail.split(None, 1)[0] if detail else "applescript"
                if code not in ("mid_mismatch",):
                    code = "applescript"
                results[rid] = (code, detail)
    for rid in planned:
        results.setdefault(rid, ("no_result", "script produced no line for this id"))
    return results


def _expected_state(actions: list[PlanAction], msg: PlanMessage,
                    target: dict | None) -> dict:
    exp: dict = {}
    for a in actions:
        if a.action == "mark_read":
            exp["read"] = 1
        elif a.action == "mark_unread":
            exp["read"] = 0
        elif a.action == "flag":
            # Calibrated live 2026-07-28: on EWS accounts Mail collapses
            # flag colors to Exchange's binary follow-up flag (flag index 2
            # wrote flag_color 1). Verify the flagged bit only; the color
            # is best-effort and reported in `observed` for the curious.
            exp["flagged"] = 1
        elif a.action == "unflag":
            exp["flagged"] = 0
        elif a.action == "move_to":
            exp["relocated_to"] = target["mailbox_rowid"]
        elif a.action == "delete":
            exp["gone_from"] = msg.mailbox_rowid
    return exp


def _check_one(s: dict | None, exp: dict, msg: PlanMessage,
               locate_fn, gmail_like: bool) -> bool:
    """Does the fresh snapshot satisfy the expected state?"""
    if "relocated_to" in exp:
        tgt = exp["relocated_to"]
        if tgt is None:
            # Target mailbox not yet in the index (fresh, unsynced): the
            # strongest checkable claim is "the message left its source".
            return s is None or bool(s["deleted"]) \
                or s["mailbox_rowid"] != msg.mailbox_rowid
        if s is not None and s["mailbox_rowid"] == tgt and not s["deleted"]:
            return True  # outcome (a): same ROWID, new mailbox
        reinserted = (
            msg.global_message_id is not None
            and locate_fn(msg.global_message_id, tgt) is not None
        )
        if not reinserted:
            return False
        if gmail_like:
            return True  # label semantics: original row legitimately persists
        return s is None or bool(s["deleted"]) \
            or s["mailbox_rowid"] != msg.mailbox_rowid
    if "gone_from" in exp:
        return s is None or bool(s["deleted"]) \
            or s["mailbox_rowid"] != exp["gone_from"]
    if s is None:
        return False
    for key in ("read", "flagged", "flag_color"):
        if key in exp and s[key] != exp[key]:
            return False
    return True


def _observed(s: dict | None) -> dict:
    if s is None:
        return {"row": "gone"}
    return {k: s[k] for k in ("read", "flagged", "flag_color",
                              "mailbox_rowid", "deleted")}


def _verify(source, plan: Plan, acted: set[int]) -> dict:
    locate_fn = getattr(source, "locate_by_gmid", lambda *_: None)
    snap_fn = source.triage_snapshot
    by_id = {m.rowid: m for m in plan.messages}
    unresolved = set(acted)
    verified: list[int] = []
    polls = 0
    interval = config.triage_verify_interval()
    for polls in range(1, config.triage_verify_polls() + 1):
        # Check first, sleep only between rounds: write-through is often
        # instant, and the pre-poll sleep dominated small plans (measured
        # 2.2 s fixed overhead on a 0.16 s mutation).
        if polls > 1 and interval:
            time.sleep(interval)
        snap = snap_fn(list(unresolved))
        for rid in list(unresolved):
            msg = by_id[rid]
            exp = _expected_state(plan.actions, msg, plan.target)
            gmail_like = "gmail" in (plan.target or {}).get("url", "").lower() \
                or msg.scheme == "imap" and "gmail" in msg.mailbox.lower()
            if _check_one(snap.get(rid), exp, msg, locate_fn, gmail_like):
                verified.append(rid)
                unresolved.discard(rid)
        if not unresolved:
            break
    pending = []
    if unresolved:
        snap = snap_fn(list(unresolved))
        for rid in sorted(unresolved):
            msg = by_id[rid]
            pending.append({
                "id": str(rid),
                "expected": _expected_state(plan.actions, msg, plan.target),
                "observed": _observed(snap.get(rid)),
            })
    return {"verified": sorted(verified), "pending": pending,
            "polls_used": polls}


def apply_plan(source, plan_id: str) -> dict:
    plans.gc()
    plan = plans.claim(plan_id)
    if plan is None:
        existing = plans.load(plan_id)
        if existing is None:
            raise TriageError("plan_not_found",
                              f"no plan with id {plan_id!r}.")
        if existing.status in ("applied", "failed", "expired"):
            raise TriageError("plan_already_applied"
                              if existing.status != "expired" else "plan_expired",
                              f"plan {plan_id} is {existing.status}.")
        raise TriageError("plan_claimed",
                          f"plan {plan_id} is being applied by another process.")

    started = time.monotonic()
    now = plans.utcnow()
    if now > datetime.fromisoformat(plan.expires_at):
        plans.expire(plan)
        raise TriageError(
            "plan_expired",
            f"plan {plan_id} expired at {plan.expires_at} (TTL "
            f"{config.triage_ttl_seconds()}s) — re-run triage_plan.",
        )

    # Pre-flight: enumerate Mail's account ids (launches Mail as a side
    # effect); abort BEFORE any mutation if a plan account is missing.
    try:
        pre = _run_osascript(_render_preflight(), timeout=15)
    except FileNotFoundError:
        plans.finish(plan, "failed", {"error": "osascript unavailable"})
        raise TriageError("osascript_unavailable",
                          "osascript not found — this tool is macOS-only.")
    except subprocess.TimeoutExpired:
        plans.finish(plan, "failed", {"error": "Mail unresponsive in pre-flight"})
        raise TriageError("mail_unresponsive",
                          "Mail.app did not answer the pre-flight within 15s.")
    if pre.returncode != 0:
        code = _osa_error_code(pre.stderr)
        plans.finish(plan, "failed", {"error": (pre.stderr or "").strip()[:300]})
        if code == -1743:
            raise TriageError(
                "automation_denied",
                "Mail.app automation is not authorised — System Settings → "
                "Privacy & Security → Automation.",
            )
        if code == -1728:
            raise TriageError("no_app", "Mail.app is not reachable.")
        raise TriageError("script_error",
                          f"pre-flight failed: {(pre.stderr or '').strip()[:200]}")
    known = {line.strip() for line in (pre.stdout or "").splitlines() if line.strip()}
    needed = {m.account for m in plan.messages if m.scheme != "local"}
    if plan.target and _scheme(plan.target["url"]) != "local":
        needed.add(plan.target["account"])
    missing = needed - known
    if missing:
        plans.finish(plan, "failed",
                     {"error": f"accounts not in Mail: {sorted(missing)}"})
        raise TriageError(
            "account_unresolvable",
            f"account id(s) {sorted(missing)} not present in Mail.app.",
        )

    # ACT — one batched script.
    planned_ids = [m.rowid for m in plan.messages]
    script = _render_script(plan)
    timeout = _auto_timeout(len(planned_ids))
    osa_started = time.monotonic()
    timed_out = False
    try:
        proc = _run_osascript(script, timeout=timeout)
        stdout = proc.stdout or ""
        if proc.returncode != 0 and not stdout.strip():
            plans.finish(plan, "failed",
                         {"error": (proc.stderr or "").strip()[:300]})
            raise TriageError(
                "script_error",
                f"batch script failed wholesale: {(proc.stderr or '').strip()[:200]}",
            )
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout = ""  # lost — VERIFY below independently reconciles
    osa_ms = int((time.monotonic() - osa_started) * 1000)

    results = _parse_batch_output(stdout, planned_ids)
    if timed_out:
        results = {rid: ("batch_timeout",
                         f"osascript killed at {timeout:.0f}s; "
                         "verification may still confirm this message")
                   for rid in planned_ids}
    acted = {rid for rid, (code, _) in results.items() if code == "ok"}
    failures = [
        {"id": str(rid), "code": code, "detail": detail}
        for rid, (code, detail) in sorted(results.items()) if code != "ok"
    ]

    # VERIFY — on timeout, check everything; mutations that landed get
    # upgraded to verified even though the script's stdout was lost.
    to_verify = set(planned_ids) if timed_out else acted
    ver = _verify(source, plan, to_verify)
    if timed_out:
        acted = set(ver["verified"])
        failures = [f for f in failures if int(f["id"]) not in acted]

    status = "applied" if acted or ver["verified"] else "failed"
    note = None
    if plan.target and plan.target.get("mailbox_rowid") is None:
        note = (
            "target mailbox was not yet index-synced at plan time; "
            "verification is limited to departure from the source mailbox. "
            "On Exchange, a move into a just-created folder can be reverted "
            "server-side — prefer moving after mailbox_create reports "
            "index_verified=true (observed live 2026-07-28)."
        )
    result = {
        "ok": True,
        "note": note,
        "plan_id": plan.id,
        "status": status,
        "planned": len(planned_ids),
        "acted": len(acted),
        "failures": failures,
        "verified": len(ver["verified"]),
        "pending": ver["pending"],
        "osascript_ms": osa_ms,
        "verify_polls": ver["polls_used"],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    plans.finish(plan, status, result)
    _log.info("triage apply %s: %s/%s acted, %s verified, %s failures",
              plan.id, len(acted), len(planned_ids), len(ver["verified"]),
              len(failures))
    return result


# --------------------------------------------------------------------- #
# mailbox_create                                                        #
# --------------------------------------------------------------------- #


def _mailbox_message_count(spec: str) -> int | None:
    """Live message count for a mailbox specifier; None = could not tell."""
    script = (
        'tell application "Mail"\n'
        f"    return (count of messages of {spec}) as text\n"
        "end tell\n"
    )
    try:
        proc = _run_osascript(script, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def _gui_delete_mailbox(spec: str) -> tuple[bool, str | None]:
    """Tier-2 deletion: drive Mail's actual 'Delete Mailbox…' menu item via
    System Events — the only deterministic deletion path (the AppleScript
    verb is fire-and-forget-maybe; measured live 2026-07-28: -10000 with
    nothing deleted, while the menu path removes the .mbox within seconds).

    Side effects: brings Mail frontmost and may open a viewer window for
    ~3 s. Requires Accessibility permission for the host process; returns
    (False, "accessibility_denied") when macOS blocks System Events.
    English menu title assumed ('Delete Mailbox…')."""
    script = (
        'tell application "Mail"\n'
        "    reopen\n"
        "    activate\n"
        "    delay 1\n"
        "    if (count of message viewers) is 0 then\n"
        "        make new message viewer\n"
        "        delay 2\n"
        "    end if\n"
        f"    set selected mailboxes of message viewer 1 to {{{spec}}}\n"
        "    delay 1\n"
        "end tell\n"
        'tell application "System Events"\n'
        '    tell process "Mail"\n'
        '        click (first menu item of menu "Mailbox" of menu bar 1 '
        'whose name begins with "Delete Mailbox")\n'
        "        delay 1\n"
        "        if exists sheet 1 of window 1 then\n"
        '            click button "Delete" of sheet 1 of window 1\n'
        '            return "DELETED"\n'
        "        end if\n"
        '        return "NO_SHEET"\n'
        "    end tell\n"
        "end tell\n"
    )
    try:
        proc = _run_osascript(script, timeout=45)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, "ui path timed out"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        code = _osa_error_code(stderr)
        if code in (-1719, -25211, -1743):
            return False, "accessibility_denied"
        return False, stderr[:200]
    return (proc.stdout or "").strip() == "DELETED", None


def delete_mailbox(source, account: str, path: str) -> dict:
    """Delete an EMPTY mailbox. Robustness knowledge baked in (fleet-tested
    2026-07-28): Mail's AppleScript `delete` verb on a mailbox often returns
    -10000 ("AppleEvent handler failed") even when the deletion SUCCEEDED —
    so the error is treated as advisory and the outcome is decided by a
    live existence re-probe, never by the verb's reply and never by the
    Envelope Index (which lags and lies for young mailboxes)."""
    resolve_fn = getattr(source, "resolve_mailbox", None)
    mailboxes_fn = getattr(source, "mailboxes", None)
    if resolve_fn is None or mailboxes_fn is None:
        raise TriageError("unsupported_source",
                          "this email source does not support triage.")
    known_accounts = {mb.account for mb in mailboxes_fn()}
    if account not in known_accounts:
        raise TriageError(
            "unknown_account",
            f"account {account!r} not found (known: {sorted(known_accounts)}).",
        )
    sample = next((mb for mb in mailboxes_fn() if mb.account == account), None)
    scheme = _scheme(getattr(sample, "path", "")) if sample else ""
    spec = _mailbox_specifier(scheme, account, path)

    # Truth = live probe. Absent already → idempotent success.
    if not _mailbox_exists_in_mail(scheme, account, path):
        return {"ok": True, "account": account, "path": path,
                "existed": False, "deleted": False, "mail_verified": True,
                "warning": None}

    # Empty-only guard: refuse to delete anything holding mail.
    count = _mailbox_message_count(spec)
    if count is None:
        raise TriageError(
            "mail_unresponsive",
            f"could not count messages in {path!r} — refusing to delete blind.",
        )
    if count > 0:
        raise TriageError(
            "not_empty",
            f"mailbox {path!r} holds {count} message(s) — only empty "
            "mailboxes can be deleted. Triage the messages out first.",
        )

    script = (
        'tell application "Mail"\n'
        f"    delete {spec}\n"
        "end tell\n"
    )
    swallowed: str | None = None
    try:
        proc = _run_osascript(script, timeout=30)
    except subprocess.TimeoutExpired:
        proc = None
        swallowed = "osascript timeout (30s); outcome decided by re-probe"
    if proc is not None and proc.returncode != 0:
        code = _osa_error_code(proc.stderr)
        if code == -1743:
            raise TriageError("automation_denied",
                              "Mail.app automation is not authorised.")
        # -10000 and friends: the verb frequently errors even on success —
        # note it and let the re-probe decide.
        swallowed = (proc.stderr or "").strip()[:200]

    def _gone() -> bool:
        for attempt in range(max(1, config.triage_verify_polls())):
            if attempt and config.triage_verify_interval():
                time.sleep(config.triage_verify_interval())
            if not _mailbox_exists_in_mail(scheme, account, path):
                return True
        return False

    method = "applescript"
    gone = _gone()
    ui_error: str | None = None
    if not gone:
        # Tier 2: the deterministic path — Mail's own Delete Mailbox menu.
        ui_ok, ui_error = _gui_delete_mailbox(spec)
        method = "ui"
        if ui_error == "accessibility_denied":
            return {"ok": False, "account": account, "path": path,
                    "existed": True, "deleted": False, "mail_verified": True,
                    "code": "accessibility_denied",
                    "error": ("AppleScript's delete verb did not take effect "
                              "and the UI fallback needs Accessibility "
                              "permission — System Settings → Privacy & "
                              "Security → Accessibility for the host app.")}
        gone = _gone()

    if gone:
        note = None
        if method == "ui":
            note = ("AppleScript delete verb had no effect; removed via "
                    "Mail's own Delete Mailbox menu (UI scripting)")
        elif swallowed:
            note = (f"Mail reported '{swallowed}' but the mailbox is "
                    "verifiably gone (known false-error on delete)")
        return {"ok": True, "account": account, "path": path,
                "existed": True, "deleted": True, "mail_verified": True,
                "method": method, "warning": note}
    return {"ok": False, "account": account, "path": path,
            "existed": True, "deleted": False, "mail_verified": True,
            "code": "delete_failed",
            "error": (f"Mail still shows {path!r} after both the delete verb "
                      f"({swallowed or 'no error'}) and the UI path "
                      f"({ui_error or 'ran without effect'}). On Exchange "
                      "accounts this usually means a phantom folder the "
                      "server never knew — remove it in Mail.app/OWA.")}


def create_mailbox(source, account: str, path: str) -> dict:
    resolve_fn = getattr(source, "resolve_mailbox", None)
    mailboxes_fn = getattr(source, "mailboxes", None)
    if resolve_fn is None or mailboxes_fn is None:
        raise TriageError("unsupported_source",
                          "this email source does not support triage.")
    known_accounts = {mb.account for mb in mailboxes_fn()}
    if account not in known_accounts:
        raise TriageError(
            "unknown_account",
            f"account {account!r} not found (known: {sorted(known_accounts)}).",
        )
    if resolve_fn(account, path) is not None:
        return {"ok": True, "account": account, "path": path,
                "existed": True, "applescript": None, "index_verified": True,
                "mail_verified": True, "warning": None}  # same shape everywhere

    # Scheme: local accounts create app-level mailboxes.
    sample = next((mb for mb in mailboxes_fn() if mb.account == account), None)
    is_local = sample is not None and _scheme(getattr(sample, "path", "")) == "local"
    lit_path = _as_literal(path)
    if is_local:
        spec = f"mailbox {lit_path}"
        make = f"make new mailbox with properties {{name:{lit_path}}}"
    else:
        lit_acct = _as_literal(account)
        spec = f"mailbox {lit_path} of account id {lit_acct}"
        make = (f"tell account id {lit_acct}\n"
                f"        make new mailbox with properties {{name:{lit_path}}}\n"
                f"    end tell")
    script = (
        'tell application "Mail"\n'
        f"    if exists {spec} then\n"
        '        return "EXISTS"\n'
        "    end if\n"
        f"    {make}\n"
        f"    if exists {spec} then\n"
        '        return "OK"\n'
        "    end if\n"
        '    return "MADE_UNVERIFIED"\n'
        "end tell\n"
    )
    try:
        proc = _run_osascript(script, timeout=30)
    except subprocess.TimeoutExpired:
        raise TriageError("mail_unresponsive", "mailbox_create timed out (30s).")
    if proc.returncode != 0:
        code = _osa_error_code(proc.stderr)
        if code == -1743:
            raise TriageError("automation_denied",
                              "Mail.app automation is not authorised.")
        raise TriageError("script_error",
                          f"mailbox_create failed: {(proc.stderr or '').strip()[:200]}")
    verdict = (proc.stdout or "").strip()
    # mail_verified = the live in-script `exists` check; the last word on
    # whether the mailbox is real client-side. A MADE_UNVERIFIED verdict
    # gets one fresh probe before we call it unverified.
    mail_verified = verdict in ("OK", "EXISTS")
    if not mail_verified:
        mail_verified = _mailbox_exists_in_mail(
            "local" if is_local else "x", account, path)

    index_verified = False
    for _ in range(config.triage_verify_polls()):
        if config.triage_verify_interval():
            time.sleep(config.triage_verify_interval())
        if resolve_fn(account, path) is not None:
            index_verified = True
            break
    warning = None
    if sample is not None and _scheme(getattr(sample, "path", "")) == "ews":
        # Observed live 2026-07-28: a folder created via AppleScript on an
        # Exchange account looked fine client-side but never existed on the
        # server — Exchange silently bounced every message moved into it,
        # and the folder could not even be deleted via AppleScript.
        warning = (
            "Exchange (EWS) account: folders created via AppleScript may not "
            "persist server-side — moves into them get reverted. Create "
            "Exchange folders in Mail.app or OWA instead, then triage into "
            "them once they appear in search results."
        )
    return {"ok": True, "account": account, "path": path,
            "existed": verdict == "EXISTS", "applescript": verdict,
            "index_verified": index_verified, "mail_verified": mail_verified,
            "warning": warning}
