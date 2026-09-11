---
name: clauseai
description: "Generate startup legal documents from attorney-drafted templates: NDAs, MSAs, DPAs, privacy policies, offer letters, and more. Download PDF, ODT, or Markdown through MCP or an agent skill. No account required."
---

# ClauseAI

ClauseAI fills General Legal CC0 templates from a short list of
fields. Do not rewrite the document; only the published fields
are fillable. Fill in as many fields as you can from what you
already know before asking the user anything. Every field is
optional, so the user can skip any question and download
immediately.

## Endpoints

- List templates: `GET https://clauseai.exe.xyz/api/templates`
- Field schema: `GET https://clauseai.exe.xyz/api/templates/{slug}`
- Generate: `POST https://clauseai.exe.xyz/api/templates/{slug}/generate`
- MCP: `https://clauseai.exe.xyz/mcp`
- GrokBot: `https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R`

## Generate request

```json
{
  "answers": {"company_name": "Acme Inc."},
  "format": "pdf",
  "email": "optional@example.com",
  "response": "json"
}
```

`format` is `pdf`, `odt`, or `markdown`. `response` of `json`
returns base64 content; omit it or use `file` for a download.
Email is optional. If the user gives one, include it.
Choice fields may include `__omit__`. Send that value to
remove a placeholder. Empty strings are unanswered, not omit.

## Workflow

1. List templates and pick the best slug.
2. Fetch the field schema.
3. Fill in as many answers as you can yourself before asking
   the user anything. Use the conversation, the user's files and
   codebase, and anything you know about their company: name,
   legal entity, domain, address, contact emails, governing
   state, and today's date for effective dates. Make reasonable
   inferences.
4. Only ask the user for fields you genuinely cannot determine.
   Do it in one short message that also shows the values you
   inferred so they can correct them. Never block on missing
   answers; every field is optional.
5. POST generate with whatever answers you have.
6. Return the file to the user.

These documents are templates, not legal advice.
