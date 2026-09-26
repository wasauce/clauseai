# ClauseAI

Generate startup legal documents from attorney-drafted templates: NDAs, MSAs, DPAs, privacy policies, offer letters, and more. Download PDF, ODT, or Markdown through MCP or an agent skill. No account required.

The gallery, JSON API, MCP server, and agent skill all share the same template engine. Templates come from [General Legal](https://general.legal) and are released under CC0.

## Use with an agent

```bash
npx skills add wasauce/clauseai --skill clauseai
```

MCP (Streamable HTTP, no account):

```
https://clauseai.exe.xyz/mcp
```

GrokBot version:

```
https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R
```

Walkthrough: [Generate a mutual NDA from your company details](https://clauseai.exe.xyz/examples/generate-nda.md).

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
| Agent index | `GET /llms.txt` |
| NDA walkthrough | `GET /examples/generate-nda.md` |
| Terms of Use | `GET /terms` |
| Privacy Policy | `GET /privacy` |

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
