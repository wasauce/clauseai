# ClauseAI plugin evaluation

Run these in a new chat with the ClauseAI plugin enabled. Record the skill used, the tools called, and the result.

## Direct

These should call `list_templates`, `get_template_fields`, and `generate_document`.

- Draft a mutual NDA between Acme Inc. and Northwind Labs, effective today.
  - Expect slug `mutual-nda`, the known names and today's date filled, and a download link.
- Create a master services agreement for Acme Inc. as the provider.
  - Expect slug `master-services-agreement` and a download link.
- Draft a US privacy policy for Acme Inc.
  - Expect slug `privacy-policy-us` and a download link.
- Write an employee offer letter from Acme Inc. to Jordan Lee.
  - Expect slug `employee-offer-letter` and a download link.

## Indirect

- We're talking to a vendor and need confidentiality both ways.
  - Expect slug `mutual-nda`, not a one-way NDA.

## Incomplete

- I need a one-way NDA.
  - Expect one question that shows any inferred values, then a generated `one-way-nda` even if the user skips the rest. Every field is optional.

## Out of scope

These should not generate a document.

- Write a custom Series A term sheet from scratch.
  - Expect a refusal and the list of available templates.
- Is this NDA enforceable in California?
  - Expect a refusal of legal advice.
- Email the NDA to the other party.
  - Expect a refusal to send the document.
