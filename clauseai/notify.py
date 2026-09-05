"""Optional Slack notifications for document generations."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from clauseai.log import get_logger

logger = get_logger(__name__)


def build_slack_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an Incoming Webhook payload for a generation event."""
    title = payload.get("template_title") or payload.get(
        "template_slug", "ClauseAI document"
    )
    email = payload.get("email")
    title_text = f"ClauseAI document generated: {title}"
    if email:
        title_text = f"{title_text} ({email})"

    fields: list[dict[str, Any]] = [
        {"title": "Template", "value": str(title), "short": True},
        {
            "title": "Slug",
            "value": str(payload.get("template_slug") or "unknown"),
            "short": True,
        },
        {
            "title": "Format",
            "value": str(payload.get("format") or "unknown"),
            "short": True,
        },
        {
            "title": "Source",
            "value": str(payload.get("source") or "unknown"),
            "short": True,
        },
    ]
    if email:
        fields.append({"title": "Email", "value": str(email), "short": False})
    else:
        fields.append({"title": "Email", "value": "not provided", "short": False})

    answers = payload.get("answers") or {}
    if isinstance(answers, dict) and answers:
        answer_lines = [f"• {key}: {value}" for key, value in answers.items()]
        fields.append(
            {
                "title": "Answers",
                "value": "\n".join(answer_lines)[:3000],
                "short": False,
            }
        )
    else:
        fields.append(
            {
                "title": "Answers",
                "value": "(none — downloaded without filling fields)",
                "short": False,
            }
        )

    if payload.get("ip_address"):
        fields.append(
            {
                "title": "IP Address",
                "value": str(payload["ip_address"]),
                "short": True,
            }
        )
    if payload.get("user_agent"):
        user_agent = str(payload["user_agent"])
        if len(user_agent) > 100:
            user_agent = user_agent[:100] + "..."
        fields.append({"title": "User Agent", "value": user_agent, "short": False})

    return {
        "text": title_text,
        "attachments": [{"fields": fields}],
    }


async def notify_generation(payload: dict[str, Any]) -> bool:
    """Post a generation notification if SLACK_WEBHOOK_URL is set."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.debug("No Slack webhook configured; skipping notification")
        return False

    message = build_slack_message(payload)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=message)
            response.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send ClauseAI Slack notification")
        return False


def dumps_payload(payload: dict[str, Any]) -> str:
    """Serialize a generation payload for logs."""
    return json.dumps(payload, default=str)
