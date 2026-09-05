"""Download General Legal templates and emit draft ClauseAI manifests.

Fetches the public CC0 templates from General-Legal/legal-templates into
``clauseai/data/templates/<slug>/``. Existing curated ``manifest.json``
files are left in place so re-runs only refresh ``template.md`` and
``README.md``. A ``marks.json`` sidecar is always rewritten so field
indices can be re-checked after an upstream update.

Usage:
    uv run python scripts/sync_templates.py
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path

logger = logging.getLogger("clauseai.sync_templates")

REPO_RAW = "https://raw.githubusercontent.com/General-Legal/legal-templates/main"
TEMPLATES_INDEX = (
    "https://api.github.com/repos/General-Legal/legal-templates/" "contents/templates"
)
OUTPUT_DIR = (
    Path(__file__).resolve().parent.parent / "clauseai" / "data" / "templates"
)
MARK_RE = re.compile(r"<mark>(.*?)</mark>", re.DOTALL)
SOURCE_BASE = "https://github.com/General-Legal/legal-templates/tree/main/templates"

# Fallback list if the GitHub API is rate-limited.
KNOWN_SLUGS = [
    "advisor-agreement",
    "business-associate-agreement",
    "cookie-notice",
    "dpa-global",
    "dpa-us",
    "employee-offer-letter",
    "master-services-agreement",
    "mutual-nda",
    "one-way-nda",
    "privacy-policy-gdpr",
    "privacy-policy-us",
    "terms-of-use",
]


def _fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "clauseai-sync/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        return response.read().decode("utf-8")


def list_slugs() -> list[str]:
    """Return template slugs from the GitHub API, or the known fallback."""
    try:
        payload = json.loads(_fetch(TEMPLATES_INDEX))
        slugs = [
            item["name"]
            for item in payload
            if item.get("type") == "dir" and item.get("name")
        ]
        if slugs:
            return sorted(slugs)
    except Exception as exc:
        logger.warning(f"GitHub API listing failed, using fallback: {exc}")
    return list(KNOWN_SLUGS)


def parse_readme_meta(readme: str, slug: str) -> dict[str, str]:
    """Pull title, category, and overview from a template README."""
    title = slug.replace("-", " ").title()
    category = "Agreements"
    description = ""

    heading = re.search(r"^#\s+(.+)$", readme, re.MULTILINE)
    if heading:
        title = heading.group(1).strip()

    cat = re.search(r"\*\*Category:\*\*\s*(.+)", readme)
    if cat:
        category = cat.group(1).strip()

    overview = re.search(
        r"## Overview\s*\n+(.+?)(?:\n## |\Z)",
        readme,
        re.DOTALL,
    )
    if overview:
        description = re.sub(r"\s+", " ", overview.group(1)).strip()

    return {
        "slug": slug,
        "title": title,
        "category": category,
        "description": description,
        "source_url": f"{SOURCE_BASE}/{slug}",
    }


def extract_marks(template_md: str) -> list[dict[str, object]]:
    """Return every <mark> span with its occurrence index."""
    marks: list[dict[str, object]] = []
    for index, match in enumerate(MARK_RE.finditer(template_md)):
        marks.append(
            {
                "index": index,
                "text": match.group(1),
            }
        )
    return marks


def draft_fields(marks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Group identical mark text into a single draft field."""
    grouped: dict[str, list[int]] = {}
    order: list[str] = []
    for mark in marks:
        text = str(mark["text"])
        key = text.strip().lower()
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(int(mark["index"]))

    fields: list[dict[str, object]] = []
    for ordinal, key in enumerate(order, start=1):
        indices = grouped[key]
        sample = next(
            str(mark["text"]) for mark in marks if int(mark["index"]) == indices[0]
        )
        label = re.sub(r"\s+", " ", sample).strip() or f"Field {ordinal}"
        if len(label) > 80:
            label = label[:77] + "..."
        fields.append(
            {
                "key": f"field_{ordinal}",
                "label": label,
                "question": f"Value for: {label}",
                "type": "text",
                "required": False,
                "marks": indices,
            }
        )
    return fields


def write_if_missing(path: Path, content: str) -> None:
    if path.exists():
        logger.info(f"Leaving curated file in place: {path.name}")
        return
    path.write_text(content, encoding="utf-8")
    logger.info(f"Wrote {path}")


def sync_slug(slug: str) -> None:
    dest = OUTPUT_DIR / slug
    dest.mkdir(parents=True, exist_ok=True)

    template_md = _fetch(f"{REPO_RAW}/templates/{slug}/template.md")
    (dest / "template.md").write_text(template_md, encoding="utf-8")

    readme = ""
    try:
        readme = _fetch(f"{REPO_RAW}/templates/{slug}/README.md")
        (dest / "README.md").write_text(readme, encoding="utf-8")
    except Exception as exc:
        logger.warning(f"No README for {slug}: {exc}")

    marks = extract_marks(template_md)
    (dest / "marks.json").write_text(
        json.dumps(marks, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    meta = parse_readme_meta(readme, slug)
    draft = {
        **meta,
        "fields": draft_fields(marks),
    }
    write_if_missing(
        dest / "manifest.json",
        json.dumps(draft, indent=2, ensure_ascii=False) + "\n",
    )
    logger.info(f"Synced {slug}: {len(marks)} marks")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slugs = list_slugs()
    logger.info(f"Syncing {len(slugs)} templates into {OUTPUT_DIR}")
    for slug in slugs:
        sync_slug(slug)
    logger.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
