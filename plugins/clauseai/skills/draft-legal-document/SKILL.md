---
name: draft-legal-document
description: "Fill an attorney-drafted startup legal template and download it. Use when the user wants an NDA, MSA, DPA, privacy policy, terms of use, cookie notice, offer letter, advisor agreement, or business associate agreement, including indirect requests such as needing confidentiality both ways."
---

# Draft a legal document

Use this skill when the user wants a startup legal document from the ClauseAI catalog. Follow an explicit user instruction when it conflicts with this skill. Still fill only published fields, and still decline legal advice, custom contracts, and sending the document to someone else.

These documents are templates, not legal advice. Do not rewrite clauses or add terms that are not already in the template.

## Tools

Use the ClauseAI MCP server, in this order:

1. Call `list_templates` and pick the one slug that matches the request.
2. Call `get_template_fields` for that slug.
3. Call `generate_document` with the answers you have.

`format` is `pdf`, `docx`, `odt`, or `markdown`. Use `pdf` unless the user asks for another format; use `docx` when they ask for Word. The tool returns filled markdown for your summary, a `download_url` for the file, and an `editable_download_url` for a Word copy. Give the user both links.

## Fill fields before asking

Fill every field you can from the conversation, the user's files, and what you know about their company: name, legal entity, domain, address, contact emails, governing state, and today's date for effective dates.

Ask only for fields you genuinely cannot determine. Do it in one short message that also shows the values you inferred, so the user can correct them. Do not block on missing answers. Every field is optional. Generate with whatever you have.

Choice fields may include `__omit__`. Send that value to remove a placeholder. An empty string leaves the placeholder; it does not omit it.

## Answers

Answers are short values, not clauses. `get_template_fields` gives each field's `max_length`.

- Use only keys from `get_template_fields`. Leave out fields you cannot answer. Do not send placeholder text such as `TBD` or `[Company Name]`.
- Choice fields take one of their `options` exactly as written.
- Send dates as `YYYY-MM-DD`. They are printed in long form.
- Do not put extra terms, such as governing law or GDPR clauses, into a field. Tell the user to add those to the downloaded document with their attorney.

If `generate_document` returns an error, fix every item it lists and call it again. Call it once per document. Call it again only when the user asks for a different format.

When a template lacks a field the user needs, or no template fits, call `send_feedback` with a short description. Do not include document text or personal details.

## Result

Tell the user which template you used, which fields you filled, and give them `download_url`. Also give them `editable_download_url`, a Word copy they can edit to fill the remaining placeholders before signing. Tell them which fields in `unfilled_fields` are still placeholders, and pass on anything in `warnings`. Summarize the document. Paste the full text only when they ask to see it.

## Decline

Decline these requests:

- A contract or policy that is not in `list_templates`. Say which templates are available instead.
- Legal advice, including whether a document is enforceable or which terms to choose.
- Negotiation, filing, or sending the document to another person.

Do not invent a substitute document for a request you declined.
