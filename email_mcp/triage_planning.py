"""Reviewable triage-plan construction, independent of action execution."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Callable

from . import audit, config, plans
from .domain.errors import ToolError
from .plans import Plan, PlanAction, PlanMessage
from .sources.base import SearchQuery

RELOCATING = {"move_to", "delete"}
ACTIONS = {"move_to", "mark_read", "mark_unread", "flag", "unflag"}
DESTRUCTIVE = {"delete"}


class TriageError(ToolError):
    def __init__(self, code: str, message: str):
        super().__init__(message, code=code)


def parse_actions(
    raw: list[dict] | None,
    allowed: set[str] = ACTIONS,
) -> list[PlanAction]:
    if not raw:
        raise TriageError(
            "invalid_action", "`actions` is required (non-empty list).",
        )
    parsed: list[PlanAction] = []
    for item in raw:
        if not isinstance(item, dict) or "action" not in item:
            raise TriageError(
                "invalid_action", f"malformed action entry: {item!r}",
            )
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
                raise TriageError(
                    "invalid_action", f"flag color {color} not in 0..6.",
                )
        parsed.append(PlanAction(action=verb, mailbox=mailbox, color=color))

    verbs = [action.action for action in parsed]
    if len(verbs) != len(set(verbs)):
        raise TriageError(
            "conflicting_actions", "duplicate actions in one plan.",
        )
    for pair in (
        {"mark_read", "mark_unread"},
        {"flag", "unflag"},
        {"move_to", "delete"},
    ):
        if pair <= set(verbs):
            raise TriageError(
                "conflicting_actions", f"{' + '.join(sorted(pair))} conflict.",
            )
    parsed.sort(key=lambda action: action.action in RELOCATING)
    return parsed


def summary(
    messages: list[PlanMessage],
    actions: list[PlanAction],
    target: dict | None,
) -> str:
    del target
    verbs = " + ".join(
        action.action + (
            f" '{action.mailbox}'" if action.action == "move_to" else ""
        )
        for action in actions
    )
    senders = len({message.from_addr for message in messages})
    dates = sorted(message.date[:10] for message in messages)
    accounts = {message.account for message in messages}
    return (
        f"{verbs}: {len(messages)} msg(s), {senders} sender(s), "
        f"{dates[0]}…{dates[-1]}, account(s) {', '.join(sorted(accounts))}"
    )


def scheme(url: str) -> str:
    return url.split("://", 1)[0] if "://" in url else ""


class TriagePlanner:
    def __init__(
        self,
        mailbox_exists: Callable[[str, str, str], bool],
        validate_literal: Callable[[str], str],
        logger,
    ) -> None:
        self._mailbox_exists = mailbox_exists
        self._validate_literal = validate_literal
        self._log = logger

    def build(
        self,
        source,
        query: SearchQuery,
        actions: list[dict] | None,
        allowed: set[str] = ACTIONS,
    ) -> Plan:
        plans.gc()
        parsed = parse_actions(actions, allowed=allowed)
        snapshot = getattr(source, "triage_snapshot", None)
        resolve_mailbox = getattr(source, "resolve_mailbox", None)
        if snapshot is None or resolve_mailbox is None:
            raise TriageError(
                "unsupported_source", "this email source does not support triage.",
            )

        cap = config.triage_max_messages()
        references = source.search(query)
        if not references:
            raise TriageError("empty_selection", "no messages match the query.")
        if len(references) > cap:
            raise TriageError(
                "selection_too_large",
                f"query matched more than {cap} messages (the cap; counting "
                "stopped there) — narrow the query, or raise "
                "EMAIL_MCP_TRIAGE_MAX if the selection is genuinely intended.",
            )

        rowids = [int(reference.id) for reference in references]
        snapshots = snapshot(rowids)
        target: dict | None = None
        move = next(
            (action for action in parsed if action.action == "move_to"), None,
        )
        if move is not None:
            accounts = {reference.account for reference in references}
            if len(accounts) > 1:
                raise TriageError(
                    "cross_account",
                    f"selection spans {len(accounts)} accounts "
                    f"({', '.join(sorted(accounts))}); move_to needs one — "
                    "add the account= filter to pick which.",
                )
            account = next(iter(accounts))
            hit = resolve_mailbox(account, move.mailbox)
            if hit is not None:
                target = {
                    "account": account,
                    "mailbox": move.mailbox,
                    "mailbox_rowid": hit[0],
                    "url": hit[1],
                }
                if all(
                    snapshots.get(rowid, {}).get("mailbox_rowid") == hit[0]
                    for rowid in rowids
                ):
                    raise TriageError(
                        "noop_move",
                        "every selected message is already in that mailbox.",
                    )
            else:
                mailbox_scheme = scheme(
                    snapshots[rowids[0]]["mailbox_url"]
                )
                if not self._mailbox_exists(
                    mailbox_scheme, account, move.mailbox,
                ):
                    raise TriageError(
                        "unknown_mailbox",
                        f"mailbox {move.mailbox!r} not found in account "
                        f"{account} (neither in the index nor in Mail.app).",
                    )
                target = {
                    "account": account,
                    "mailbox": move.mailbox,
                    "mailbox_rowid": None,
                    "url": f"{mailbox_scheme}://{account}/?unsynced",
                }

        messages: list[PlanMessage] = []
        for reference in references:
            rowid = int(reference.id)
            snapshot_row = snapshots.get(rowid)
            if snapshot_row is None:
                continue
            self._validate_literal(reference.mailbox)
            messages.append(PlanMessage(
                rowid=rowid,
                account=reference.account,
                scheme=scheme(snapshot_row["mailbox_url"]),
                mailbox=reference.mailbox,
                mailbox_rowid=snapshot_row["mailbox_rowid"],
                subject=reference.subject,
                from_addr=reference.from_addr,
                date=reference.date.isoformat(),
                unread=reference.unread,
                message_id_header=snapshot_row["mid_header"],
                global_message_id=snapshot_row["gmid"],
                pre={
                    "read": snapshot_row["read"],
                    "flagged": snapshot_row["flagged"],
                    "flag_color": snapshot_row["flag_color"],
                },
            ))
        if not messages:
            raise TriageError(
                "empty_selection",
                "all matched messages vanished before planning.",
            )

        now = plans.utcnow()
        plan = Plan(
            id=plans.new_id(now),
            created_at=plans.iso(now),
            expires_at=plans.iso(
                now + timedelta(seconds=config.triage_ttl_seconds())
            ),
            status="draft",
            query={
                key: value for key, value in vars(query).items()
                if value not in (None, "", False, 0)
            },
            actions=parsed,
            target=target,
            messages=messages,
            summary=summary(messages, parsed, target),
        )
        plans.save(plan)
        detail: dict = {
            "count": len(plan.messages),
            "actions": [
                {key: value for key, value in vars(action).items()
                 if value is not None}
                for action in plan.actions
            ],
        }
        if plan.target is not None:
            detail["target"] = plan.target
        audit.emit(
            "plan_create", outcome="created", operation_id=plan.id,
            plan_id=plan.id, summary=plan.summary, detail=detail,
        )
        self._log.info("triage plan %s: %s", plan.id, plan.summary)
        return plan

    @staticmethod
    def delete_max() -> int:
        return config.triage_delete_max()

    def build_delete(self, source, query: SearchQuery) -> Plan:
        narrowed = replace(query, exclude_trash=True, from_exact=True)
        references = source.search(narrowed)
        accounts = {reference.account for reference in references}
        if len(accounts) > 1:
            raise TriageError(
                "cross_account",
                f"selection spans {len(accounts)} accounts "
                f"({', '.join(sorted(accounts))}); delete needs one — "
                "add the account= filter to pick which.",
            )
        cap = self.delete_max()
        if len(references) > cap:
            raise TriageError(
                "selection_too_large",
                f"query matched more than {cap} messages (the delete cap) — "
                "narrow the query, or raise EMAIL_MCP_TRIAGE_DELETE_MAX if "
                "the deletion is genuinely intended.",
            )
        return self.build(
            source, narrowed, [{"action": "delete"}],
            allowed=ACTIONS | DESTRUCTIVE,
        )
