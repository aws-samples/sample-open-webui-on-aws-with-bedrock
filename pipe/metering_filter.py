# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
title: Usage Metering
id: metering
description: Captures per-message token usage to the metering EventBridge bus and shows soft-quota warnings. Part of the opt-in metering module (docs/METERING.md); enforcement lives at the gateway, not here.
version: 1.0.0
license: MIT-0
"""

# Global filter function seeded by pipe/metering_seed.py when the metering
# module is enabled. Two jobs, both deliberately non-blocking:
#
#   outlet() — the METERING capture point. Open WebUI hands the completed
#     message list (including the normalized per-message `usage` the app
#     persisted from the stream) to outlet filters after every chat turn, for
#     every model — native connections and pipes alike. We forward one compact
#     usage event per assistant message to the metering EventBridge bus. The
#     debit Lambda (infra) settles it against the interceptor's admission
#     estimate, keyed by the same idempotency scheme (design §4.1/§4.2).
#
#   inlet() — the SOFT-WARN UX. Reads a cached quota snapshot from DynamoDB
#     (own counter row only) and emits a toast at >= the warn threshold.
#     WARN-ONLY BY DESIGN: the inlet never raises — the gateway interceptor is
#     the sole enforcement wall (a raise here from a stale snapshot would
#     outlive operator resets; see 02-DESIGN §4.2 E3).
#
# Failure contract: metering must never break chat. Every code path swallows
# its own exceptions, logs one line, and increments Metering/CaptureFailures.

__filter_version__ = "1.0.0"

import json
import logging
import os
import time

log = logging.getLogger(__name__)

from pydantic import BaseModel, Field

try:
    import boto3
except Exception:  # pragma: no cover - boto3 ships in the official image
    boto3 = None

EVENT_SOURCE = "openwebui.metering"
EVENT_DETAIL_TYPE = "usage"


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=100, description="Filter execution priority (runs after content filters).")
        METERING_BUS: str = Field(
            default=os.environ.get("METERING_BUS", ""),
            description="EventBridge bus name for usage events. Defaults to the METERING_BUS task env var set by the CDK metering stack.",
        )
        METERING_TABLE: str = Field(
            default=os.environ.get("METERING_TABLE", ""),
            description="DynamoDB table for the inlet's soft-warn quota snapshot (read-only, own row).",
        )
        AWS_REGION_NAME: str = Field(
            default=os.environ.get("METERING_REGION", os.environ.get("AWS_REGION", "us-east-1")),
            description="Region for the bus/table clients.",
        )
        WARN_SNOOZE_SECONDS: int = Field(
            default=3600,
            description="Do not repeat the soft-warn toast for the same user within this window.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._events = None
        self._ddb = None
        self._warned_at: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # lazy clients (filter loads at app boot; region/bus come from env)
    # ------------------------------------------------------------------ #

    def _events_client(self):
        if self._events is None and boto3 is not None:
            self._events = boto3.client("events", region_name=self.valves.AWS_REGION_NAME)
        return self._events

    def _ddb_client(self):
        if self._ddb is None and boto3 is not None:
            self._ddb = boto3.client("dynamodb", region_name=self.valves.AWS_REGION_NAME)
        return self._ddb

    def _emit_capture_failure(self, err: Exception, where: str):
        log.warning(f"metering_filter: {where} failed (chat unaffected): {err.__class__.__name__}: {err}")

    @staticmethod
    def _oauth_sub(user: dict) -> str:
        """The Cognito subject — the SAME identity the gateway interceptor
        counters key on. Open WebUI's UserModel carries it as
        oauth = {"<provider>": {"sub": "..."}} (observed in v0.10.x; no flat
        oauth_sub field as of that release — the nested walk plus the fallback
        below tolerate shape changes). Fall back to the app-internal user id
        (attribution still works, but won't join with interceptor-side
        counters)."""
        oauth = user.get("oauth") or {}
        if isinstance(oauth, dict):
            for entry in oauth.values():
                if isinstance(entry, dict) and entry.get("sub"):
                    return str(entry["sub"])
        return str(user.get("id") or "unknown")

    @staticmethod
    async def _group_names(user: dict) -> list:
        """The user's Open WebUI groups (synced from cognito:groups when OAuth
        group management is on). model_dump() has no groups field, so query
        the Groups table; failure just means 'unassigned'."""
        try:
            from open_webui.models.groups import Groups

            groups = await Groups.get_groups_by_member_id(user.get("id") or "")
            return [g.name for g in groups]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ #
    # inlet — soft-warn toast only; NEVER raises
    # ------------------------------------------------------------------ #

    async def inlet(self, body: dict, __user__: dict | None = None, __event_emitter__=None) -> dict:
        try:
            if not (self.valves.METERING_TABLE and __user__ and __event_emitter__):
                return body
            sub = self._oauth_sub(__user__ or {})
            if not sub or sub == "unknown":
                return body
            now = time.time()
            if now - self._warned_at.get(sub, 0) < self.valves.WARN_SNOOZE_SECONDS:
                return body
            ddb = self._ddb_client()
            if ddb is None:
                return body
            window = time.strftime("%Y-%m")
            resp = ddb.get_item(
                TableName=self.valves.METERING_TABLE,
                Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
                ProjectionExpression="used_usd, est_usd, soft_limit_usd, hard_limit_usd",
            )
            item = resp.get("Item")
            if not item:
                return body
            used = float(item.get("used_usd", {}).get("N", "0")) + float(item.get("est_usd", {}).get("N", "0"))
            soft = float(item.get("soft_limit_usd", {}).get("N", "0"))
            hard = float(item.get("hard_limit_usd", {}).get("N", "0"))
            if soft > 0 and used >= soft:
                pct = int(100 * used / hard) if hard > 0 else 100
                self._warned_at[sub] = now
                await __event_emitter__(
                    {
                        "type": "notification",
                        "data": {
                            "type": "warning",
                            "content": f"You've used {min(pct, 100)}% of your monthly AI policy. "
                            "Future requests may be blocked after recorded usage reaches 100%; "
                            "this is not a guaranteed billing ceiling.",
                        },
                    }
                )
        except Exception as e:  # noqa: BLE001 — warn UX must never break chat
            self._emit_capture_failure(e, "inlet")
        return body

    # ------------------------------------------------------------------ #
    # outlet — the capture point; forwards usage events, never raises
    # ------------------------------------------------------------------ #

    async def outlet(self, body: dict, __user__: dict | None = None, __metadata__: dict | None = None) -> dict:
        try:
            bus = self.valves.METERING_BUS
            events = self._events_client()
            if not bus or events is None:
                return body

            user = __user__ or {}
            meta = __metadata__ or {}
            group_names = await self._group_names(user)
            entries = []
            # outlet receives the FULL message list every turn — only the last
            # assistant message is this turn's completion (earlier ones were
            # already captured on their own turns; settle idempotency would
            # drop re-emits anyway, but don't send them at all).
            assistant_msgs = [m for m in body.get("messages", []) if m.get("role") == "assistant"]
            for msg in assistant_msgs[-1:]:
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                detail = {
                    "sub": self._oauth_sub(user),
                    "user_id": user.get("id") or "unknown",
                    "groups": group_names,
                    "model": body.get("model") or msg.get("model") or "unknown",
                    # Lane the request took at the gateway. Open WebUI's chat
                    # turn is always the chat-completions lane; the debit Lambda
                    # will override this with the authoritative lane from the
                    # matched admission estimate when one is found (design §4.2).
                    # Emitting it here keeps filter-only / no-estimate rows
                    # (e.g. the capture canary) from showing "unknown".
                    "lane": "chat/completions",
                    "chat_id": body.get("chat_id") or meta.get("chat_id") or "",
                    "message_id": msg.get("id") or "",
                    # provider response id when the app surfaced one (idempotency key #1)
                    "response_id": (usage.get("id") or msg.get("info", {}).get("id") or "") if isinstance(msg.get("info"), dict) else usage.get("id") or "",
                    "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                    "cached_tokens": int(
                        (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                        or usage.get("cache_read_input_tokens")
                        or 0
                    ),
                    "source": "FILTER",
                    "ts": int(time.time()),
                }
                if detail["input_tokens"] == 0 and detail["output_tokens"] == 0:
                    continue
                entries.append(
                    {
                        "Source": EVENT_SOURCE,
                        "DetailType": EVENT_DETAIL_TYPE,
                        "Detail": json.dumps(detail),
                        "EventBusName": bus,
                    }
                )
            if not entries:
                return body

            resp = events.put_events(Entries=entries[:10])
            failed = resp.get("FailedEntryCount", 0)
            if failed:
                # retry the failures once, then count and move on — never raise
                retry = [e for e, r in zip(entries[:10], resp.get("Entries", [])) if r.get("ErrorCode")]
                if retry:
                    resp2 = events.put_events(Entries=retry)
                    if resp2.get("FailedEntryCount", 0):
                        self._emit_capture_failure(
                            RuntimeError(f"{resp2['FailedEntryCount']} usage events undelivered after retry"),
                            "outlet.put_events",
                        )
        except Exception as e:  # noqa: BLE001 — capture must never break chat
            self._emit_capture_failure(e, "outlet")
        return body
