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
    client_name = payload.get("mcp_client")
    if client_name:
        client_value = str(client_name)
        client_version = payload.get("mcp_client_version")
        if client_version:
            client_value = f"{client_value} {client_version}"
        fields.append({"title": "Client", "value": client_value, "short": True})
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
    if payload.get("origin"):
        fields.append(
            {"title": "Origin", "value": str(payload["origin"]), "short": False}
        )

    return {
        "text": title_text,
        "attachments": [{"fields": fields}],
    }


def _slack_escape(text: str) -> str:
    """Escape Slack control characters so feedback cannot ping a channel."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_feedback_slack_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an Incoming Webhook payload for a feedback submission."""
    category = str(payload.get("category") or "other")
    slug = payload.get("template_slug")
    email = payload.get("email")
    title_text = f"ClauseAI feedback ({category})"
    if slug:
        title_text = f"{title_text}: {slug}"

    fields: list[dict[str, Any]] = [
        {
            "title": "Message",
            "value": _slack_escape(str(payload.get("message") or ""))[:3000],
            "short": False,
        },
        {"title": "Category", "value": category, "short": True},
        {"title": "Template", "value": str(slug or "not given"), "short": True},
        {
            "title": "Source",
            "value": str(payload.get("source") or "unknown"),
            "short": True,
        },
        {
            "title": "Email",
            "value": _slack_escape(str(email)) if email else "not provided",
            "short": True,
        },
    ]
    client_name = payload.get("mcp_client")
    if client_name:
        client_value = str(client_name)
        client_version = payload.get("mcp_client_version")
        if client_version:
            client_value = f"{client_value} {client_version}"
        fields.append(
            {"title": "Client", "value": _slack_escape(client_value), "short": True}
        )
    if payload.get("ip_address"):
        fields.append(
            {
                "title": "IP Address",
                "value": _slack_escape(str(payload["ip_address"])),
                "short": True,
            }
        )
    if payload.get("user_agent"):
        user_agent = str(payload["user_agent"])
        if len(user_agent) > 100:
            user_agent = user_agent[:100] + "..."
        fields.append(
            {"title": "User Agent", "value": _slack_escape(user_agent), "short": False}
        )
    if payload.get("origin"):
        fields.append(
            {
                "title": "Origin",
                "value": _slack_escape(str(payload["origin"])),
                "short": False,
            }
        )

    return {
        "text": title_text,
        "attachments": [{"fields": fields}],
    }


async def notify_generation(payload: dict[str, Any]) -> bool:
    """Post a generation notification if SLACK_WEBHOOK_URL is set."""
    return await _post_to_slack(build_slack_message(payload))


async def notify_feedback(payload: dict[str, Any]) -> bool:
    """Post a feedback notification if SLACK_WEBHOOK_URL is set."""
    return await _post_to_slack(build_feedback_slack_message(payload))


async def _post_to_slack(message: dict[str, Any]) -> bool:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.debug("No Slack webhook configured; skipping notification")
        return False

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
