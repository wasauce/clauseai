# ClauseAI

Fill attorney-drafted [General Legal](https://general.legal) CC0 templates from a short list of questions and download PDF, OpenDocument, or Markdown. No registration is required.

The gallery, JSON API, MCP server, and agent skill all share the same template engine.

## Run locally

System dependencies:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `pandoc` for ODT export (`brew install pandoc` or `apt-get install pandoc`)
- WeasyPrint libraries for PDF (`brew install pango gdk-pixbuf libffi` on macOS)

```bash
cp env.example .env
uv sync --dev
uv run python run.py
```

Open [http://localhost:8000](http://localhost:8000).

| Surface | Path |
| --- | --- |
| Gallery | `GET /` |
| Wizard | `GET /{slug}` |
| JSON API | `GET /api/templates` |
| Generate | `POST /api/templates/{slug}/generate` |
| Agent skill | `GET /skill.md` |
| MCP | `/mcp` |

## Tests

```bash
uv run pytest
```

## Refresh templates

Vendored markdown lives in `clauseai/data/templates/`. Curated `manifest.json` files are kept across re-syncs:

```bash
uv run python scripts/sync_templates.py
```

Templates are for general reference only and are not legal advice.
