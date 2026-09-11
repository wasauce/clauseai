# Generate a mutual NDA from your company details

ClauseAI fills attorney-drafted startup legal templates. This walkthrough
generates a mutual NDA as Markdown from a company name and effective date.
No account is required.

## Install the skill

```bash
npx skills add wasauce/clauseai --skill clauseai
```

Or point an MCP client at the hosted Streamable HTTP server:

```
https://clauseai.exe.xyz/mcp
```

GrokBot version:

```
https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R
```

## MCP flow

1. Call `list_templates` and pick `mutual-nda`.
2. Call `get_template_fields` with slug `mutual-nda`. The fill-in fields are
   `company_name` and `effective_date`.
3. Call `generate_document` with:

```json
{
  "slug": "mutual-nda",
  "answers": {
    "company_name": "Acme Inc.",
    "effective_date": "2026-09-11"
  },
  "format": "markdown"
}
```

The same request works over HTTP:

```bash
curl -sS https://clauseai.exe.xyz/api/templates/mutual-nda/generate \
  -H "Content-Type: application/json" \
  -d '{
    "answers": {
      "company_name": "Acme Inc.",
      "effective_date": "2026-09-11"
    },
    "format": "markdown",
    "response": "json"
  }'
```

## Sample output

The returned Markdown starts like this (placeholders replaced, rest of the
CC0 template omitted):

```markdown
**MUTUAL NON-DISCLOSURE AGREEMENT**

**This Mutual Non-Disclosure Agreement** (this "***Agreement***") is
entered into between Acme Inc., a Delaware corporation ("***Company***")
and the other party named on the signature page hereto ("***Other
Signatory***") as of September 11, 2026 (the "***Effective Date***"),
to protect the confidentiality of certain confidential information of
Company or of Other Signatory to be disclosed under this Agreement
solely for use in evaluating or pursuing a business relationship
between the parties (the "***Permitted Use***").
```

Templates are attorney-drafted by [General Legal](https://general.legal)
and released under CC0. They are not legal advice.
