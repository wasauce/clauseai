"""Tests for ClauseAI templates, rendering, pages, API, Slack, and logs."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from clauseai.mcp import get_template_fields, list_templates
from clauseai.notify import build_slack_message, notify_generation
from clauseai.service import (
    OMIT_ANSWER,
    InvalidFormatError,
    TemplateField,
    TemplateNotFoundError,
    answered_fields,
    fill_template,
    generate_document,
    generation_payload,
    list_template_slugs,
    load_template,
    normalize_format,
    record_generation,
    render_document,
)

CCPA_TABLE_SKELETON = "| [INSERT] | [INSERT] | [INSERT] | [INSERT] | [INSERT] |"
CCPA_TABLE_ROW = "| Contact data | Identifiers | Purposes | Providers | None |"


def test_all_manifests_match_template_marks() -> None:
    """Every curated mark index must exist in the vendored template."""
    slugs = list_template_slugs()
    assert len(slugs) == 12
    for slug in slugs:
        loaded = load_template(slug)
        assert loaded.mark_count > 0
        mapped: set[int] = set()
        for field in loaded.manifest.fields:
            if not field.marks:
                assert field.replaces
                continue
            for index in field.marks:
                assert 0 <= index < loaded.mark_count
                assert index not in mapped
                mapped.add(index)


def test_fill_template_replaces_answers_and_strips_marks() -> None:
    """Answered marks become values; unanswered marks keep placeholder text."""
    filled = fill_template(
        "mutual-nda",
        {"company_name": "Acme Inc."},
    )
    assert "between Acme Inc., a Delaware corporation" in filled
    assert "Acme Inc.]" not in filled
    assert "<mark>" not in filled
    assert "2026" in filled


def test_fill_template_escapes_html_in_answers() -> None:
    """HTML in answers must not reach md2pdf/WeasyPrint as raw tags."""
    payload = '<img src="http://169.254.169.254/latest/meta-data/">'
    filled = fill_template("mutual-nda", {"company_name": payload})
    assert "<img" not in filled
    assert "&lt;img" in filled
    assert "src=&quot;http://169.254.169.254/latest/meta-data/&quot;" in filled


def test_fill_template_neutralizes_markdown_images() -> None:
    """Markdown images in answers must not become HTML <img> during PDF render."""
    payload = "![x](http://169.254.169.254/)"
    filled = fill_template("mutual-nda", {"company_name": payload})
    assert "![x](http://169.254.169.254/)" not in filled
    assert "&#33;[x](http://169.254.169.254/)" in filled


def test_mutual_nda_fills_signature_company_name() -> None:
    """company_name fills the opening party name and the signature line."""
    filled = fill_template("mutual-nda", {"company_name": "Acme Inc."})
    assert filled.count("Acme Inc.") == 2
    assert "[Company]" not in filled
    assert "<mark>" not in filled


def test_one_way_nda_fills_signature_company_name() -> None:
    """company_name fills the opening party name and the signature line."""
    filled = fill_template("one-way-nda", {"company_name": "Acme Inc."})
    assert filled.count("Acme Inc.") == 2
    assert "[Company Name]" not in filled
    assert "<mark>" not in filled


def test_one_way_nda_permitted_use_omits_editorial_brackets() -> None:
    """A custom permitted_use must not leave a stray closing bracket."""
    filled = fill_template(
        "one-way-nda",
        {"permitted_use": "evaluate an acquisition"},
    )
    assert "evaluate an acquisition" in filled
    assert "evaluate an acquisition]" not in filled
    assert "solely to enable Recipient to evaluate an acquisition (" in filled


@pytest.mark.parametrize("slug", ["dpa-us", "dpa-global"])
def test_dpa_company_name_fills_signature(slug: str) -> None:
    """company_name fills the intro, signature block, and Annex 1 name."""
    filled = fill_template(slug, {"company_name": "Acme Inc."})
    assert filled.count("Acme Inc.") == 3
    assert "[CompanyName]" not in filled
    assert "<mark>" not in filled


def test_cookie_notice_omits_unfilled_provider_columns() -> None:
    """Cookie category table keeps types, not unfinished [ADD] cells."""
    filled = fill_template(
        "cookie-notice",
        {
            "company_name": "Acme Inc.",
            "website_label": "website",
            "include_app": "; and our mobile app (App)",
            "analytics_provider": "FullStory",
            "contact_email": "privacy@acme.com",
            "effective_date": "September 5, 2026",
        },
    )
    assert "[ADD]" not in filled
    assert "| Type | Description |" in filled
    assert "Who serves the cookies" not in filled
    assert "How to control them" not in filled
    assert "in the chart" not in filled
    for category in (
        "Advertising",
        "Analytics",
        "Essential",
        "Functionality / performance",
        "Social",
    ):
        assert f"| {category} |" in filled


@pytest.mark.parametrize(
    "slug,short_name_count,expect_controller",
    [
        ("privacy-policy-us", 5, False),
        ("privacy-policy-gdpr", 6, True),
    ],
)
def test_privacy_policy_replaces_every_company_name(
    slug: str, short_name_count: int, expect_controller: bool
) -> None:
    """Answered company-name fields drop editorial brackets
    and leftover placeholders."""
    filled = fill_template(
        slug,
        {
            "full_company_name": "Acme, Inc.",
            "company_name": "Acme",
            "business_description": "cloud software",
        },
    )
    assert "[CompanyName]" not in filled
    assert "[Acme, Inc.]" not in filled
    assert "[Acme]" not in filled
    assert "[cloud software]" not in filled
    assert "how Acme processes personal information" in filled
    assert filled.count("Acme, Inc.") == 1
    assert filled.replace("Acme, Inc.", "").count("Acme") == short_name_count
    if expect_controller:
        assert "Controller. Acme is the controller" in filled
    else:
        assert "Controller. Acme is the controller" not in filled


@pytest.mark.parametrize(
    "slug,email_count",
    [
        ("privacy-policy-us", 4),
        ("privacy-policy-gdpr", 4),
    ],
)
def test_privacy_policy_ccpa_table_is_fillable(slug: str, email_count: int) -> None:
    """CCPA chart rows are a published field, not leftover [INSERT] cells."""
    keys = {field.key for field in load_template(slug).manifest.fields}
    assert "ccpa_disclosure_table" in keys

    filled = fill_template(
        slug,
        {
            "ccpa_disclosure_table": CCPA_TABLE_ROW,
            "company_email": "legal@acme.com",
        },
    )
    assert CCPA_TABLE_ROW in filled
    assert CCPA_TABLE_SKELETON not in filled
    assert filled.count("legal@acme.com") == email_count


PRIVACY_POLICY_SLUGS = ("privacy-policy-us", "privacy-policy-gdpr")
PRIVACY_CHOICE_KEYS = (
    "targeted_advertising",
    "profiling",
    "sells_personal_info",
    "minors_personal_info",
    "sensitive_personal_info",
)
INSERT_DESCRIPTION = "[INSERT DESCRIPTION]"
PROFILING_INSERT_HOLE = "decision-making that [INSERT DESCRIPTION]"
PROFILING_DESCRIPTION_EXAMPLE = "determines eligibility for premium features"


def _privacy_choice_fields(slug: str) -> dict[str, TemplateField]:
    return {
        field.key: field
        for field in load_template(slug).manifest.fields
        if field.key in PRIVACY_CHOICE_KEYS
    }


def _published_choice_text(slug: str, chosen: str, **answers: str) -> str:
    """Apply companion token substitutions the fill engine would apply."""
    text = chosen
    for field in load_template(slug).manifest.fields:
        if not field.replaces or field.replaces not in text:
            continue
        value = answers.get(field.key, "").strip()
        fill = value if value else f"[{field.label}]"
        text = text.replace(field.replaces, fill)
    return text


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_schema_exposes_practice_choices(slug: str) -> None:
    """State-law alternatives are wizard/MCP choice fields, not unmapped marks."""
    fields = _privacy_choice_fields(slug)
    assert set(fields) == set(PRIVACY_CHOICE_KEYS)
    for field in fields.values():
        assert field.type == "choice"
        assert len(field.options) >= 2


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_schema_exposes_profiling_description(slug: str) -> None:
    """The yes-branch hole is a collectible field, not leftover INSERT text."""
    fields = {field.key: field for field in load_template(slug).manifest.fields}
    companion = fields["profiling_description"]
    assert companion.type == "textarea"
    assert companion.replaces == INSERT_DESCRIPTION
    assert companion.marks == []


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_choices_render_one_branch(slug: str) -> None:
    """Selecting a practice option publishes that branch and not its siblings."""
    for field in _privacy_choice_fields(slug).values():
        for chosen in field.options:
            filled = fill_template(slug, {field.key: chosen})
            published = _published_choice_text(slug, chosen)
            assert published in filled
            assert PROFILING_INSERT_HOLE not in filled
            for other in field.options:
                if other != chosen:
                    assert other not in filled
                    assert _published_choice_text(slug, other) not in filled


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_unanswered_choices_are_not_contradictory(slug: str) -> None:
    """Skipped choices stay as labels instead of both legal sentences."""
    filled = fill_template(slug, {})
    assert "<mark>" not in filled
    assert PROFILING_INSERT_HOLE not in filled
    for field in _privacy_choice_fields(slug).values():
        assert f"[{field.label}]" in filled
        for option in field.options:
            assert option not in filled


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_profiling_description_interpolates(slug: str) -> None:
    """Yes plus a description publishes a complete affirmative disclosure."""
    profiling = _privacy_choice_fields(slug)["profiling"]
    yes_option, no_option = profiling.options[0], profiling.options[1]
    filled = fill_template(
        slug,
        {
            "profiling": yes_option,
            "profiling_description": PROFILING_DESCRIPTION_EXAMPLE,
        },
    )
    expected = _published_choice_text(
        slug,
        yes_option,
        profiling_description=PROFILING_DESCRIPTION_EXAMPLE,
    )
    assert expected in filled
    assert PROFILING_INSERT_HOLE not in filled
    assert "[Profiling description]" not in filled
    assert no_option not in filled


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_profiling_description_placeholder(slug: str) -> None:
    """Yes without a description uses the companion label, not INSERT."""
    profiling = _privacy_choice_fields(slug)["profiling"]
    yes_option = profiling.options[0]
    filled = fill_template(slug, {"profiling": yes_option})
    expected = _published_choice_text(slug, yes_option)
    assert expected in filled
    assert PROFILING_INSERT_HOLE not in filled
    assert "[Profiling description]" in filled


@pytest.mark.parametrize("slug", PRIVACY_POLICY_SLUGS)
def test_privacy_policy_profiling_no_branch_omits_description(slug: str) -> None:
    """The negative sentence is published without a description hole."""
    profiling = _privacy_choice_fields(slug)["profiling"]
    yes_option, no_option = profiling.options[0], profiling.options[1]
    filled = fill_template(
        slug,
        {
            "profiling": no_option,
            "profiling_description": PROFILING_DESCRIPTION_EXAMPLE,
        },
    )
    assert no_option in filled
    assert PROFILING_INSERT_HOLE not in filled
    assert yes_option not in filled
    assert _published_choice_text(slug, yes_option) not in filled
    assert PROFILING_DESCRIPTION_EXAMPLE not in filled


def test_privacy_policy_wizard_exposes_practice_choices(
    simple_client: TestClient,
) -> None:
    """The wizard lists a select for every privacy-practice alternative."""
    for slug in PRIVACY_POLICY_SLUGS:
        response = simple_client.get(f"/{slug}")
        assert response.status_code == 200
        for key in PRIVACY_CHOICE_KEYS:
            assert f'id="field_{key}"' in response.text
        assert 'id="field_profiling_description"' in response.text


def test_cookie_notice_analytics_provider_is_consistent() -> None:
    """Session-replay follow-up uses the same provider as the first mention."""
    filled = fill_template("cookie-notice", {"analytics_provider": "Hotjar"})
    assert filled.count("Hotjar") == 3
    assert "FullStory" not in filled
    assert "<mark>" not in filled

    default = fill_template("cookie-notice", {})
    assert "FullStory" in default
    assert "<mark>" not in default


def test_empty_choice_option_becomes_omit_sentinel() -> None:
    """Blank choice options are stored as the nonempty omit sentinel."""
    field = TemplateField(
        key="include_app",
        label="Include a mobile app?",
        question="Does this notice also cover a mobile app?",
        type="choice",
        options=["keep", ""],
        marks=[0],
    )
    assert field.options == ["keep", OMIT_ANSWER]


def test_cookie_notice_omit_clears_app_clause() -> None:
    """Explicit omit removes the app clause; unanswered keeps a label."""
    unanswered = fill_template("cookie-notice", {})
    assert "[Include a mobile app?]" in unanswered
    assert OMIT_ANSWER not in unanswered

    empty = fill_template("cookie-notice", {"include_app": ""})
    assert "[Include a mobile app?]" in empty

    omitted = fill_template("cookie-notice", {"include_app": OMIT_ANSWER})
    assert "; and our mobile app (App)" not in omitted
    assert "mobile app (App)" not in omitted
    assert "[Include a mobile app?]" not in omitted
    assert OMIT_ANSWER not in omitted

    included = fill_template(
        "cookie-notice",
        {"include_app": "; and our mobile app (App)"},
    )
    assert "; and our mobile app (App)" in included


def test_terms_of_use_always_decisionlayer() -> None:
    """Terms of Use ships only the DecisionLayer arbitration clause."""
    filled = fill_template("terms-of-use", {"company_email": "legal@acme.com"})
    assert "DecisionLayer" in filled
    assert "JAMS" not in filled
    assert "OPTION" not in filled
    # One email answer fills notices, contact, accessibility, and opt-out.
    assert filled.count("legal@acme.com") == 5

    schema_keys = {field.key for field in load_template("terms-of-use").manifest.fields}
    assert "arbitration_forum" not in schema_keys


def test_employee_offer_letter_always_decisionlayer() -> None:
    """Employee offer letters ship only the DecisionLayer arbitration clause."""
    filled = fill_template("employee-offer-letter", {})
    assert "DecisionLayer" in filled
    assert "JAMS" not in filled
    assert "OPTION" not in filled
    assert "Select one of the following" not in filled
    assert "This agreement requires you to arbitrate" in filled


def test_terms_of_use_honors_governing_state() -> None:
    """DecisionLayer NY language covers procedure, not the Terms' governing law."""
    filled = fill_template("terms-of-use", {"governing_state": "Delaware"})
    assert "State of Delaware" in filled
    assert "These Terms are governed by the Rules" not in filled
    assert "This arbitration agreement is governed by the Rules" in filled
    assert "substantive law governing these Terms is set out in Section 10" in filled
    assert "New York County, New York" in filled
    assert "NY CPLR ARTICLE 75" in filled


def test_msa_always_decisionlayer() -> None:
    """The MSA ships DecisionLayer arbitration instead of AAA."""
    filled = fill_template(
        "master-services-agreement",
        {
            "company_name": "Acme Inc.",
            "customer_name": "Globex LLC",
            "company_email": "legal@acme.com",
        },
    )
    assert filled.count("Acme Inc.") == 3
    assert filled.count("Globex LLC") == 3
    assert "DecisionLayer" in filled
    assert "artificial intelligence" in filled
    assert "American Arbitration Association" not in filled
    assert "AAA" not in filled
    assert "www.adr.org" not in filled
    assert "New Castle County" not in filled
    assert "first contact Company at legal@acme.com" in filled
    assert filled.count("legal@acme.com") == 2
    assert "injunctive or other equitable relief" in filled

    schema_keys = {
        field.key for field in load_template("master-services-agreement").manifest.fields
    }
    assert "arbitration_venue" not in schema_keys
    assert "arbitration_forum" not in schema_keys


def test_msa_honors_governing_state() -> None:
    """DecisionLayer NY language covers procedure, not the MSA's governing law."""
    filled = fill_template(
        "master-services-agreement",
        {"governing_state": "California"},
    )
    assert "laws of the State of [California]" in filled
    assert "substantive rights and obligations of the parties shall be governed by the internal laws of the State of New York" not in filled
    assert "This Arbitration Agreement is governed by the Rules" in filled
    assert "substantive law governing this Agreement is set out in the Governing Law section" in filled
    assert "New York State" in filled


def test_employee_offer_letter_reuses_employee_name() -> None:
    """One employee_name answer fills the address block and signature."""
    filled = fill_template(
        "employee-offer-letter",
        {"employee_name": "Jane Doe"},
    )
    assert filled.count("Jane Doe") == 2
    assert "[Employee Name]" not in filled


def test_employee_offer_letter_maps_opt_out_contact() -> None:
    """Published fields still leave the opt-out blank until contact is given."""
    schema_keys = {
        field.key for field in load_template("employee-offer-letter").manifest.fields
    }
    assert "opt_out_contact" in schema_keys

    published = {
        "company_name": "Acme",
        "letter_date": "2026-09-05",
        "employee_name": "Jane Doe",
        "employee_address": "1 Main St",
        "employee_first_name": "Jane",
        "position": "Engineer",
        "duties": "write software",
        "manager": "Alex Rivera",
        "work_location": "the Company's office",
        "city": "San Francisco",
        "annual_salary": "100,000",
        "pay_cadence": "every two weeks",
    }
    unanswered = fill_template("employee-offer-letter", published)
    assert "[Insert Email or Address]" in unanswered
    assert "legal@acme.com" not in unanswered

    filled = fill_template(
        "employee-offer-letter",
        {**published, "opt_out_contact": "legal@acme.com"},
    )
    assert "[Insert Email or Address]" not in filled
    assert filled.count("legal@acme.com") == 1
    assert filled.count("Jane Doe") == 2
    assert "DecisionLayer" in filled
    assert "JAMS" not in filled


def test_employee_offer_letter_keeps_fixed_prose_outside_marks() -> None:
    """Location, salary, and cadence marks must not consume surrounding prose."""
    filled = fill_template(
        "employee-offer-letter",
        {
            "work_location": "the Company's office",
            "city": "San Francisco",
            "annual_salary": "100,000",
            "pay_cadence": "every two weeks",
        },
    )
    assert "<mark>" not in filled
    assert (
        "Your regular work location will be [the Company&#x27;s office] in "
        "[San Francisco, California]."
    ) in filled
    assert (
        "Your annual base salary will be $[100,000], subject to applicable "
        "payroll deductions"
    ) in filled
    assert "$$" not in filled
    assert "on a [every two weeks] basis." in filled


def test_fill_template_unknown_slug() -> None:
    """Unknown slugs raise TemplateNotFoundError."""
    with pytest.raises(TemplateNotFoundError):
        fill_template("not-a-real-template", {})


def test_answered_fields_ignores_empty_and_unknown_keys() -> None:
    """Only known non-empty answers are kept."""
    cleaned = answered_fields(
        "mutual-nda",
        {
            "company_name": " Acme ",
            "effective_date": "",
            "extra": "nope",
        },
    )
    assert cleaned == {"company_name": "Acme"}


def test_answered_fields_keeps_omit_sentinel() -> None:
    """The omit sentinel is a real answer, unlike an empty string."""
    cleaned = answered_fields(
        "cookie-notice",
        {
            "include_app": OMIT_ANSWER,
            "company_name": "",
        },
    )
    assert cleaned == {"include_app": OMIT_ANSWER}


def test_normalize_format_aliases() -> None:
    """Accept md aliases and reject unknown formats."""
    assert normalize_format("MD") == "markdown"
    assert normalize_format("pdf") == "pdf"
    with pytest.raises(InvalidFormatError):
        normalize_format("docx")


def test_pandoc_available_in_ci() -> None:
    """If CI=true, pandoc MUST be present so ODT conversion actually runs."""
    if not os.getenv("CI"):
        pytest.skip("Not running in CI; binary check only meaningful on CI.")
    assert shutil.which("pandoc") is not None, (
        "CI is missing the `pandoc` binary. ClauseAI ODT export tests will "
        "skip without it. Add `apt-get install -y pandoc` to the CI workflow."
    )


@pytest.mark.asyncio
async def test_render_all_formats() -> None:
    """Render the mutual NDA as markdown, PDF, and ODT."""
    markdown = fill_template("mutual-nda", {"company_name": "Acme Inc."})

    md_bytes = await render_document(markdown, "markdown")
    assert b"Acme Inc." in md_bytes

    pdf_bytes = await render_document(markdown, "pdf")
    assert pdf_bytes.startswith(b"%PDF")

    if shutil.which("pandoc") is None:
        pytest.skip(
            "pandoc binary not installed; install with `brew install pandoc` "
            "(macOS) or `apt-get install pandoc` (linux)."
        )

    odt_bytes = await render_document(markdown, "odt")
    assert odt_bytes.startswith(b"PK")


@pytest.mark.asyncio
async def test_generate_document_zero_answers() -> None:
    """Users can download a template without answering anything."""
    rendered = await generate_document("mutual-nda", {}, "markdown")
    assert rendered.filename == "mutual-nda.md"
    assert rendered.content
    assert b"<mark>" not in rendered.content


def test_gallery_and_wizard_pages(simple_client: TestClient) -> None:
    """Gallery and wizard pages render without a database."""
    gallery = simple_client.get("/")
    assert gallery.status_code == 200
    assert "ClauseAI" in gallery.text
    assert "Have your agent build your docs" in gallery.text
    assert "mutual-nda" in gallery.text

    wizard = simple_client.get("/mutual-nda")
    assert wizard.status_code == 200
    assert "Download now" in wizard.text
    assert 'id="field_company_name"' in wizard.text

    missing = simple_client.get("/not-a-template")
    assert missing.status_code == 404


def test_cookie_notice_wizard_omit_choice(simple_client: TestClient) -> None:
    """The omit choice is a nonempty sentinel, not a second empty option."""
    wizard = simple_client.get("/cookie-notice")
    assert wizard.status_code == 200
    assert 'id="field_include_app"' in wizard.text
    assert f'value="{OMIT_ANSWER}"' in wizard.text
    assert "(omit)" in wizard.text
    assert 'value=""' in wizard.text


def test_cookie_notice_omit_via_wizard_form(simple_client: TestClient) -> None:
    """Selecting omit on the wizard removes the mobile-app clause."""
    response = simple_client.post(
        "/cookie-notice/generate",
        data={
            "format": "markdown",
            "field_include_app": OMIT_ANSWER,
        },
    )
    assert response.status_code == 200
    text = response.content.decode("utf-8")
    assert "; and our mobile app (App)" not in text
    assert "[Include a mobile app?]" not in text
    assert OMIT_ANSWER not in text


def test_skill_and_api_endpoints(simple_client: TestClient) -> None:
    """Skill and JSON API endpoints are public."""
    skill = simple_client.get("/skill.md")
    assert skill.status_code == 200
    assert "name: clauseai" in skill.text
    assert (
        "Generate startup legal documents from attorney-drafted templates"
        in skill.text
    )
    assert "http://testserver/api/templates" in skill.text
    assert "Fill in as many answers as you can yourself" in skill.text
    assert "__omit__" in skill.text
    assert "https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R" in skill.text
    assert "https://clauseai.exe.xyz" not in skill.text
    assert 'description: "Generate startup legal documents' in skill.text

    well_known = simple_client.get("/.well-known/skills/clauseai/SKILL.md")
    assert well_known.status_code == 200
    assert well_known.text == skill.text
    well_known_lower = simple_client.get("/.well-known/skills/clauseai/skill.md")
    assert well_known_lower.status_code == 200
    assert well_known_lower.text == skill.text

    listing = simple_client.get("/api/templates")
    assert listing.status_code == 200
    slugs = {item["slug"] for item in listing.json()["templates"]}
    assert slugs == set(list_template_slugs())

    schema = simple_client.get("/api/templates/mutual-nda")
    assert schema.status_code == 200
    keys = {field["key"] for field in schema.json()["fields"]}
    assert keys == {"company_name", "effective_date"}


def test_gallery_and_wizard_are_indexable(simple_client: TestClient) -> None:
    """Discoverable HTML pages must not send noindex."""
    gallery = simple_client.get("/")
    assert gallery.status_code == 200
    assert "noindex" not in gallery.text
    assert "npx skills add wasauce/clauseai --skill clauseai" in gallery.text
    assert "https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R" in gallery.text
    assert (
        "Generate startup legal documents from attorney-drafted templates"
        in gallery.text
    )

    wizard = simple_client.get("/mutual-nda")
    assert wizard.status_code == 200
    assert "noindex" not in wizard.text


def test_llms_txt_and_walkthrough(simple_client: TestClient) -> None:
    """Agents can find the skill, API docs, catalog, and NDA walkthrough."""
    index = simple_client.get("/llms.txt")
    assert index.status_code == 200
    assert "http://testserver/skill.md" in index.text
    assert "http://testserver/docs" in index.text
    assert "http://testserver/api/templates" in index.text
    assert "npx skills add wasauce/clauseai --skill clauseai" in index.text
    assert "https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R" in index.text

    walkthrough = simple_client.get("/examples/generate-nda.md")
    assert walkthrough.status_code == 200
    assert "Generate a mutual NDA from your company details" in walkthrough.text
    assert "https://clauseai.exe.xyz/mcp" in walkthrough.text
    assert "npx skills add wasauce/clauseai --skill clauseai" in walkthrough.text
    assert "https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R" in walkthrough.text

    robots = simple_client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Allow: /" in robots.text
    assert "Disallow: /health" in robots.text


def test_mcp_initialize_without_trailing_slash(simple_client: TestClient) -> None:
    """Registry listings point at /mcp without a trailing slash."""
    response = simple_client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "clauseai-tests", "version": "0.0.1"},
            },
        },
    )
    assert response.status_code == 200
    assert "ClauseAI" in response.text
    assert "0.1.0" in response.text


def test_download_without_answers(simple_client: TestClient) -> None:
    """The wizard download button works with an empty form."""
    response = simple_client.post(
        "/mutual-nda/generate",
        data={"format": "markdown"},
    )
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "mutual-nda.md" in response.headers["content-disposition"]
    assert b"MUTUAL NON-DISCLOSURE AGREEMENT" in response.content


def test_wizard_generate_json_success(simple_client: TestClient) -> None:
    """JSON posts to the wizard generate endpoint still return a file."""
    response = simple_client.post(
        "/mutual-nda/generate",
        json={"answers": {"company_name": "Acme Inc."}, "format": "markdown"},
    )
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert b"Acme Inc." in response.content


def test_wizard_generate_rejects_malformed_json(
    simple_client: TestClient,
) -> None:
    """Truncated JSON is a client error, not an unhandled 500."""
    response = simple_client.post(
        "/mutual-nda/generate",
        content='{"answers":',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON body"


def test_wizard_generate_rejects_invalid_answers_shape(
    simple_client: TestClient,
) -> None:
    """Valid JSON with the wrong answers type returns 422."""
    response = simple_client.post(
        "/mutual-nda/generate",
        json={"answers": []},
    )
    assert response.status_code == 422
    assert "detail" in response.json()


def test_api_generate_json_includes_answers(simple_client: TestClient) -> None:
    """JSON generate returns base64 markdown with filled answers."""
    response = simple_client.post(
        "/api/templates/mutual-nda/generate",
        json={
            "answers": {"company_name": "Acme Inc."},
            "format": "markdown",
            "email": "founder@example.com",
            "response": "json",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "mutual-nda.md"
    text = base64.b64decode(payload["content_base64"]).decode("utf-8")
    assert "Acme Inc." in text


@pytest.mark.asyncio
async def test_record_generation_logs_and_notifies() -> None:
    """Generations are logged and forwarded to the optional notifier."""
    payload = {
        "template_slug": "mutual-nda",
        "format": "markdown",
        "source": "api",
        "answers": {"company_name": "Acme Inc."},
        "email": "ops@example.com",
    }
    with patch(
        "clauseai.notify.notify_generation",
        new_callable=AsyncMock,
        return_value=True,
    ) as notify:
        await record_generation(payload=payload)
        await asyncio.sleep(0)
        notify.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_notify_generation_skips_without_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Slack webhook means log-only, no HTTP call."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    payload = generation_payload(
        slug="mutual-nda",
        fmt="markdown",
        answers={},
        email=None,
        source="api",
        ip_address=None,
        user_agent=None,
    )
    with patch("clauseai.notify.httpx.AsyncClient") as client_cls:
        sent = await notify_generation(payload)
    assert sent is False
    client_cls.assert_not_called()


def test_slack_payload_includes_email() -> None:
    """Slack notifications include the captured email and answers."""
    payload = generation_payload(
        slug="mutual-nda",
        fmt="pdf",
        answers={"company_name": "Acme Inc."},
        email="counsel@example.com",
        source="web",
        ip_address="203.0.113.10",
        user_agent="ClauseAITests/1.0",
    )
    message = build_slack_message(payload)
    raw_fields = message["attachments"][0]["fields"]
    fields = {field["title"]: field["value"] for field in raw_fields}
    assert "counsel@example.com" in message["text"]
    assert fields["Email"] == "counsel@example.com"
    assert "Acme Inc." in fields["Answers"]
    assert fields["Source"] == "web"


def test_terms_and_privacy_pages(simple_client: TestClient) -> None:
    """Published policies are linkable and do not replace the wizards."""
    terms = simple_client.get("/terms")
    assert terms.status_code == 200
    assert "ClauseAI" in terms.text
    assert "https://clauseai.exe.xyz/privacy" in terms.text
    assert "does not require an account" in terms.text
    assert "personal or business use" in terms.text
    assert "[INSERT" not in terms.text
    assert "Cookie Policy / Privacy Policy -- hyperlink" not in terms.text
    assert "Templates are attorney-drafted" not in terms.text
    assert "noindex" not in terms.text

    privacy = simple_client.get("/privacy")
    assert privacy.status_code == 200
    assert "ClauseAI" in privacy.text
    assert "https://clauseai.exe.xyz/terms" in privacy.text
    assert "We do not sell your Personal Information" in privacy.text
    assert "Optional email address" in privacy.text
    assert "[INSERT" not in privacy.text
    assert "Cookie Policy / Privacy Policy -- hyperlink" not in privacy.text
    assert "Templates are attorney-drafted" not in privacy.text
    assert "noindex" not in privacy.text

    terms_wizard = simple_client.get("/terms-of-use")
    assert terms_wizard.status_code == 200
    assert "Download now" in terms_wizard.text

    privacy_wizard = simple_client.get("/privacy-policy-us")
    assert privacy_wizard.status_code == 200
    assert "Download now" in privacy_wizard.text

    gallery = simple_client.get("/")
    assert 'href="/terms"' in gallery.text
    assert 'href="/privacy"' in gallery.text
    assert "Templates are attorney-drafted" in gallery.text

    index = simple_client.get("/llms.txt")
    assert "http://testserver/terms" in index.text
    assert "http://testserver/privacy" in index.text


def test_mcp_list_and_fields_tools() -> None:
    """MCP tools expose the same catalog as the JSON API."""
    templates = list_templates()
    assert len(templates) == 12
    fields = get_template_fields("one-way-nda")
    assert fields["slug"] == "one-way-nda"
    assert fields["fields"]
