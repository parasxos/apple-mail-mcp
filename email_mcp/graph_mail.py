"""Microsoft Graph mailbox operations over an injected authenticated client."""
from __future__ import annotations

import base64
import urllib.parse
from datetime import datetime, timezone
from typing import Callable


class GraphMailbox:
    """Draft, deferred-send, body, and reconciliation operations.

    Authentication, HTTP retry policy, and token storage belong to the
    injected request client. This component owns only mailbox semantics.
    """

    def __init__(
        self,
        request: Callable,
        error_type: type[Exception],
        transport_error_type: type[Exception],
        logger,
        deferred_property: str,
    ) -> None:
        self._request = request
        self._error = error_type
        self._transport_error = transport_error_type
        self._log = logger
        self._deferred_property = deferred_property

    @staticmethod
    def _name(identity) -> str:
        return getattr(identity, "name", str(identity))

    @staticmethod
    def _reason(body: dict) -> str:
        error = body.get("error")
        if isinstance(error, dict):
            return f"{error.get('code')}: {error.get('message')}"
        return str(error or body)

    @staticmethod
    def _iso_utc(when: datetime) -> str:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def create_mime_draft(self, identity, raw: bytes) -> str:
        status, draft = self._request(
            "POST", "/me/messages", identity,
            body=base64.b64encode(raw), ctype="text/plain",
        )
        if status != 201 or "id" not in draft:
            raise self._error(
                f"[{self._name(identity)}/graph] draft create failed "
                f"(HTTP {status}): {self._reason(draft)}"
            )
        return str(draft["id"])

    def draft_receipt(self, identity, draft_id: str) -> dict:
        status, message = self._request(
            "GET",
            f"/me/messages/{urllib.parse.quote(draft_id)}"
            "?$select=isDraft,internetMessageId,parentFolderId",
            identity,
        )
        if status != 200:
            raise self._error(
                f"[{self._name(identity)}/graph] draft readback failed "
                f"(HTTP {status}): {self._reason(message)}"
            )
        status, folder = self._request(
            "GET", "/me/mailFolders/drafts", identity,
        )
        if status != 200 or "id" not in folder:
            raise self._error(
                f"[{self._name(identity)}/graph] cannot resolve the Drafts "
                f"folder (HTTP {status}): {self._reason(folder)}"
            )
        return {
            "is_draft": bool(message.get("isDraft")),
            "internet_message_id": str(
                message.get("internetMessageId") or ""
            ),
            "in_drafts_folder": (
                str(message.get("parentFolderId")) == str(folder["id"])
            ),
        }

    def create_deferred_draft(
        self,
        identity,
        raw: bytes,
        when: datetime,
    ) -> str:
        status, draft = self._request(
            "POST", "/me/messages", identity,
            body=base64.b64encode(raw), ctype="text/plain",
        )
        if status != 201 or "id" not in draft:
            raise self._error(
                f"[{self._name(identity)}/graph] MIME draft create failed "
                f"(HTTP {status}): {self._reason(draft)}"
            )
        draft_id = str(draft["id"])
        send_ambiguous = False
        try:
            status, body = self._request(
                "PATCH", f"/me/messages/{draft_id}", identity,
                body={"singleValueExtendedProperties": [{
                    "id": self._deferred_property,
                    "value": self._iso_utc(when),
                }]},
            )
            if status != 200:
                raise self._error(
                    f"[{self._name(identity)}/graph] deferred-send property "
                    f"rejected (HTTP {status}): {self._reason(body)}"
                )
            try:
                status, body = self._request(
                    "POST", f"/me/messages/{draft_id}/send", identity,
                )
            except self._transport_error:
                send_ambiguous = True
                raise
            if status != 202:
                raise self._error(
                    f"[{self._name(identity)}/graph] deferred /send rejected "
                    f"(HTTP {status}): {self._reason(body)}"
                )
        except Exception:
            cleanup_status: int | None = None
            try:
                cleanup_status, _ = self._request(
                    "DELETE", f"/me/messages/{draft_id}", identity,
                )
                if cleanup_status not in (204, 404):
                    self._log.warning(
                        "graph: cleanup DELETE of draft %s got HTTP %s [%s]",
                        draft_id, cleanup_status, self._name(identity),
                    )
            except Exception as cleanup:
                self._log.warning(
                    "graph: cleanup DELETE of draft %s failed: %s [%s]",
                    draft_id, cleanup, self._name(identity),
                )
            if send_ambiguous and cleanup_status != 204:
                self._log.warning(
                    "graph: /send outcome for draft %s is ambiguous (cleanup "
                    "DELETE -> %s) — treating as armed; the reconcile pass "
                    "will disambiguate [%s]",
                    draft_id, cleanup_status, self._name(identity),
                )
                return draft_id
            raise
        return draft_id

    @staticmethod
    def _body_payload(body: dict) -> dict:
        return {
            "contentType": str(body.get("contentType") or "text"),
            "content": str(body.get("content") or ""),
        }

    def fetch_body_by_message_id(
        self,
        identity,
        message_id: str,
    ) -> dict | None:
        quoted = message_id.replace("'", "''")
        query = urllib.parse.urlencode({
            "$filter": f"internetMessageId eq '{quoted}'",
            "$select": "body",
        })
        status, body = self._request(
            "GET", f"/me/messages?{query}", identity,
        )
        if status != 200:
            raise self._error(
                f"[{self._name(identity)}/graph] message lookup by "
                f"internetMessageId failed (HTTP {status}): "
                f"{self._reason(body)}"
            )
        values = body.get("value") or []
        if not values:
            return None
        return self._body_payload(values[0].get("body") or {})

    def translate_ews_ids(
        self,
        identity,
        ews_ids: list[str],
    ) -> dict[str, str]:
        if not ews_ids:
            return {}
        status, body = self._request(
            "POST", "/me/translateExchangeIds", identity,
            body={
                "inputIds": ews_ids,
                "sourceIdType": "ewsId",
                "targetIdType": "restId",
            },
        )
        if status != 200:
            raise self._error(
                f"[{self._name(identity)}/graph] translateExchangeIds failed "
                f"(HTTP {status}): {self._reason(body)}"
            )
        translated: dict[str, str] = {}
        for row in body.get("value") or []:
            source, target = row.get("sourceId"), row.get("targetId")
            if source and target:
                translated[str(source)] = str(target)
        return translated

    def fetch_body_by_graph_id(
        self,
        identity,
        rest_id: str,
    ) -> dict | None:
        quoted = urllib.parse.quote(rest_id, safe="")
        status, body = self._request(
            "GET", f"/me/messages/{quoted}?$select=body", identity,
        )
        if status == 404:
            return None
        if status != 200:
            raise self._error(
                f"[{self._name(identity)}/graph] message body fetch failed "
                f"(HTTP {status}): {self._reason(body)}"
            )
        return self._body_payload(body.get("body") or {})

    def _find_by_message_id(
        self,
        identity,
        folder: str,
        message_id: str,
    ) -> str | None:
        quoted = message_id.replace("'", "''")
        query = urllib.parse.urlencode({
            "$filter": f"internetMessageId eq '{quoted}'",
            "$select": "id",
        })
        status, body = self._request(
            "GET", f"/me/mailFolders/{folder}/messages?{query}", identity,
        )
        if status != 200:
            raise self._error(
                f"[{self._name(identity)}/graph] {folder} lookup by "
                f"internetMessageId failed (HTTP {status}): "
                f"{self._reason(body)}"
            )
        values = body.get("value") or []
        return str(values[0].get("id")) if values else None

    def find_draft_by_message_id(
        self,
        identity,
        message_id: str,
    ) -> str | None:
        return self._find_by_message_id(identity, "drafts", message_id)

    def sent_by_message_id(self, identity, message_id: str) -> bool:
        return (
            self._find_by_message_id(identity, "sentitems", message_id)
            is not None
        )

    def draft_status(
        self,
        identity,
        draft_id: str,
        message_id: str,
    ) -> str:
        status, body = self._request(
            "GET", f"/me/messages/{draft_id}?$select=id,isDraft", identity,
        )
        if status == 200 and body.get("isDraft", True):
            return "held"
        if status not in (200, 404):
            return "unknown"
        try:
            sent = self.sent_by_message_id(identity, message_id)
        except self._error:
            return "unknown"
        if sent:
            return "sent"
        return "cancelled_externally" if status == 404 else "unknown"

    def delete_draft(self, identity, draft_id: str) -> str:
        status, body = self._request(
            "DELETE", f"/me/messages/{draft_id}", identity,
        )
        if status == 204:
            return "deleted"
        if status == 404:
            return "gone"
        raise self._error(
            f"[{self._name(identity)}/graph] draft delete failed "
            f"(HTTP {status}): {self._reason(body)} — entry stays on graph; "
            "retried next pass"
        )
