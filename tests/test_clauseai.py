"""Tests for ClauseAI templates, rendering, pages, API, Slack, and logs."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from fastmcp.exceptions import ToolError

from clauseai import __version__, service
from clauseai.mcp import (
    generate_document as mcp_generate_document,
    get_template_fields,
    list_templates,
    send_feedback,
)
from clauseai.notify import (
    build_feedback_slack_message,
    build_slack_message,
    notify_generation,
)
from clauseai.service import (
    OMIT_ANSWER,
    FeedbackRateLimitError,
    InvalidAnswersError,
    InvalidFeedbackError,
    InvalidFormatError,
    TemplateField,
    TemplateNotFoundError,
    answered_fields,
    build_filename,
    fill_template,
    generate_document,
    generation_payload,
    list_template_slugs,
    load_template,
    normalize_format,
    record_generation,
    render_document,
    unfilled_fields,
    validate_answers,
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


@pytest.mark.parametrize("slug", list_template_slugs())
def test_fill_template_drops_brackets_around_answers(slug: str) -> None:
    """An answer never prints as ``[value]``, and an omitted one leaves no ``[]``."""
    manifest = load_template(slug).manifest
    empty_brackets = fill_template(slug, {}).count("[]")
    for field in manifest.fields:
        if not field.marks:
            continue
        values = field.options if field.type == "choice" else ["Sample Value"]
        for value in values:
            filled = fill_template(slug, {field.key: value})
            if value == OMIT_ANSWER:
                assert filled.count("[]") == empty_brackets, field.key
            else:
                assert f"[{value}]" not in filled, field.key


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


def test_mutual_nda_fills_other_party_entity_and_governing_law() -> None:
    """The counterparty, entity description, and governing state are fillable."""
    filled = fill_template(
        "mutual-nda",
        {
            "company_name": "Acme Inc.",
            "company_entity": "an Ohio limited liability company",
            "governing_state": "California",
            "other_party_name": "Northwind Labs",
        },
    )
    assert "Acme Inc., an Ohio limited liability company (" in filled
    assert "laws of the State of California, without" in filled
    assert "**OTHER SIGNATORY**\n- Northwind Labs\n" in filled
    assert "Delaware" not in filled
    assert "Name of Other Signatory" not in filled


def test_mutual_nda_defaults_keep_template_text() -> None:
    """Unanswered new fields print the template's original wording."""
    filled = fill_template("mutual-nda", {"company_name": "Acme Inc."})
    assert "Acme Inc., a Delaware corporation (" in filled
    assert "laws of the State of Delaware, without" in filled
    assert "- Name of Other Signatory (Please Print)" in filled


def test_one_way_nda_fills_recipient_and_governing_law() -> None:
    """The recipient's name and governing state are fillable."""
    filled = fill_template(
        "one-way-nda",
        {"governing_state": "New York", "recipient_name": "Jordan Lee"},
    )
    assert "laws of the State of New York, without" in filled
    assert "**RECIPIENT**\n- Jordan Lee\n" in filled
    assert "Delaware" not in filled


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
        field.key
        for field in load_template("master-services-agreement").manifest.fields
    }
    assert "arbitration_venue" not in schema_keys
    assert "arbitration_forum" not in schema_keys


def test_msa_honors_governing_state() -> None:
    """DecisionLayer NY language covers procedure, not the MSA's governing law."""
    filled = fill_template(
        "master-services-agreement",
        {"governing_state": "California"},
    )
    assert "laws of the State of California, without" in filled
    assert (
        "substantive rights and obligations of the parties shall be governed by the internal laws of the State of New York"
        not in filled
    )
    assert "This Arbitration Agreement is governed by the Rules" in filled
    assert (
        "substantive law governing this Agreement is set out in the Governing Law section"
        in filled
    )
    assert "New York State" in filled


def test_msa_platform_description_is_a_field() -> None:
    """The upstream client's product description is a blank, not fixed text."""
    unanswered = fill_template("master-services-agreement", {})
    assert "generative AI applications" not in unanswered
    assert "platform designed to [what the platform does]." in unanswered

    filled = fill_template(
        "master-services-agreement",
        {"platform_description": "manage customer support tickets"},
    )
    assert "platform designed to manage customer support tickets." in filled


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
        "Your regular work location will be the Company&#x27;s office in "
        "San Francisco, California."
    ) in filled
    assert (
        "Your annual base salary will be $100,000, subject to applicable "
        "payroll deductions"
    ) in filled
    assert "$$" not in filled
    assert "on a every two weeks basis." in filled


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
    """Accept md and Word aliases and reject unknown formats."""
    assert normalize_format("MD") == "markdown"
    assert normalize_format("pdf") == "pdf"
    assert normalize_format("Word") == "docx"
    assert build_filename("mutual-nda", "word") == "mutual-nda.docx"
    with pytest.raises(InvalidFormatError):
        normalize_format("rtf")


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
    """Render the mutual NDA as markdown, PDF, Word, and ODT."""
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

    docx_bytes = await render_document(markdown, "docx")
    assert docx_bytes.startswith(b"PK")


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
        "Generate startup legal documents from attorney-drafted templates" in skill.text
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
    assert keys == {
        "company_name",
        "company_entity",
        "effective_date",
        "governing_state",
        "other_party_name",
    }


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


_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _mcp_post(
    client: TestClient,
    body: dict,
    *,
    session_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
):
    """POST one MCP JSON-RPC message, keeping the streamable HTTP session."""
    headers = dict(_MCP_HEADERS)
    if session_id:
        headers["mcp-session-id"] = session_id
        headers["mcp-protocol-version"] = "2025-03-26"
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/mcp", headers=headers, json=body)


def test_mcp_initialize_without_trailing_slash(simple_client: TestClient) -> None:
    """Registry listings point at /mcp without a trailing slash."""
    response = _mcp_post(
        simple_client,
        {
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
    assert __version__ in response.text


def test_mcp_generate_records_declared_client(simple_client: TestClient) -> None:
    """A later tool call records the clientInfo from initialize."""
    from loguru import logger

    recorded: dict = {}
    connected: list[str] = []
    sink = logger.add(
        lambda message: connected.append(message.record["message"]),
        level="INFO",
    )

    async def _record(*, payload: dict) -> None:
        recorded["payload"] = payload

    caller_headers = {
        "User-Agent": "ClauseAITests/1.0",
        "Origin": "https://example.test",
        "X-Forwarded-For": "203.0.113.44",
    }
    try:
        initialize = _mcp_post(
            simple_client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "clauseai-tests", "version": "0.0.1"},
                },
            },
            extra_headers=caller_headers,
        )
        assert initialize.status_code == 200
        session_id = initialize.headers.get("mcp-session-id")
        assert session_id

        with patch("clauseai.mcp.clauseai.record_generation", new=_record):
            generated = _mcp_post(
                simple_client,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "generate_document",
                        "arguments": {
                            "slug": "mutual-nda",
                            "format": "markdown",
                            "answers": {},
                        },
                    },
                },
                session_id=session_id,
                extra_headers=caller_headers,
            )
    finally:
        logger.remove(sink)

    assert generated.status_code == 200, generated.text
    assert "mutual-nda.md" in generated.text
    assert "MCP client connected: name=clauseai-tests version=0.0.1" in connected
    payload = recorded["payload"]
    assert payload["source"] == "mcp"
    assert payload["mcp_client"] == "clauseai-tests"
    assert payload["mcp_client_version"] == "0.0.1"
    assert payload["user_agent"] == "ClauseAITests/1.0"
    assert payload["origin"] == "https://example.test"
    assert payload["ip_address"] == "203.0.113.44"


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
    assert "Client" not in fields
    assert "Origin" not in fields
    assert payload["mcp_client"] is None
    assert "origin" not in payload


def test_generation_payload_includes_mcp_client() -> None:
    """MCP generations keep the declared client, version, and origin."""
    payload = generation_payload(
        slug="mutual-nda",
        fmt="markdown",
        answers={},
        email=None,
        source="mcp",
        ip_address="203.0.113.10",
        user_agent="claude-code/1.0.5 (cli)",
        mcp_client="claude-code",
        mcp_client_version="1.0.5",
        origin="https://claude.ai",
    )
    assert payload["mcp_client"] == "claude-code"
    assert payload["mcp_client_version"] == "1.0.5"
    assert payload["origin"] == "https://claude.ai"
    assert payload["user_agent"] == "claude-code/1.0.5 (cli)"
    assert payload["ip_address"] == "203.0.113.10"

    message = build_slack_message(payload)
    raw_fields = message["attachments"][0]["fields"]
    fields = {field["title"]: field["value"] for field in raw_fields}
    assert fields["Client"] == "claude-code 1.0.5"
    assert fields["User Agent"] == "claude-code/1.0.5 (cli)"
    assert fields["Origin"] == "https://claude.ai"
    assert fields["IP Address"] == "203.0.113.10"


@pytest.mark.asyncio
async def test_record_generation_logs_mcp_client() -> None:
    """The generation log names the MCP client that requested the document."""
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]),
        level="INFO",
    )
    payload = generation_payload(
        slug="mutual-nda",
        fmt="markdown",
        answers={},
        email=None,
        source="mcp",
        ip_address=None,
        user_agent="claude-code/1.0.5 (cli)",
        mcp_client="claude-code",
        mcp_client_version="1.0.5",
        origin="https://claude.ai",
    )
    try:
        with patch(
            "clauseai.notify.notify_generation",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await record_generation(payload=payload)
            await asyncio.sleep(0)
    finally:
        logger.remove(sink)
    text = "\n".join(messages)
    assert "client=claude-code" in text
    assert "version=1.0.5" in text
    assert "origin=https://claude.ai" in text


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
    assert "October 2, 2026" in privacy.text
    assert "client name and version" in privacy.text
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
    assert len(templates.templates) == 12
    fields = get_template_fields("one-way-nda")
    assert fields.slug == "one-way-nda"
    assert fields.fields


@pytest.fixture(autouse=True)
def _reset_feedback_rate() -> None:
    """Each test starts with an empty feedback rate window."""
    service._feedback_times.clear()


def test_unknown_template_error_lists_slugs() -> None:
    """An unknown slug names the valid ones and the closest match."""
    with pytest.raises(TemplateNotFoundError) as excinfo:
        load_template("mutual-nd")
    message = str(excinfo.value)
    assert "Did you mean mutual-nda?" in message
    assert "one-way-nda" in message
    assert "employee-offer-letter" in message


def test_validate_answers_rejects_stuffed_clause() -> None:
    """A clause pasted into a short field is refused with the limit and a fix."""
    stuffed = "Strictly limited to onboarding. Governing law: Germany. " * 20
    with pytest.raises(InvalidAnswersError) as excinfo:
        validate_answers(
            "one-way-nda",
            {"company_name": "Acme Inc.", "permitted_use": stuffed},
        )
    issues = excinfo.value.issues
    assert [(issue["field"], issue["code"]) for issue in issues] == [
        ("permitted_use", "too_long")
    ]
    message = str(excinfo.value)
    assert "No document was generated" in message
    assert "the limit is 300" in message
    assert "What is the permitted use" in message
    assert "attorney" in message
    assert "send_feedback" in message


def test_validate_answers_reports_every_problem() -> None:
    """Unknown keys, bad choices, and bad types are reported together."""
    with pytest.raises(InvalidAnswersError) as excinfo:
        validate_answers(
            "employee-offer-letter",
            {
                "company": "Acme",
                "governing_law": "Germany",
                "pay_cadence": "weekly",
                "position": {"title": "Engineer"},
                "letter_date": "2026-02-30",
            },
        )
    codes = {issue["field"]: issue["code"] for issue in excinfo.value.issues}
    assert codes == {
        "company": "unknown_field",
        "governing_law": "unknown_field",
        "pay_cadence": "invalid_choice",
        "position": "wrong_type",
        "letter_date": "invalid_date",
    }
    message = str(excinfo.value)
    assert "Did you mean company_name?" in message
    assert "Valid keys: company_name, letter_date" in message
    assert '"semimonthly"' in message
    assert "5 answers need fixing" in message


def test_validate_answers_normalizes_dates_and_choices() -> None:
    """ISO dates are written out; choices match ignoring case and spacing."""
    cleaned = validate_answers(
        "employee-offer-letter",
        {
            "letter_date": "2026-10-02",
            "pay_cadence": " Semimonthly ",
            "annual_salary": 180000,
            "manager": "",
        },
    )
    assert cleaned == {
        "letter_date": "October 2, 2026",
        "annual_salary": "180000",
        "pay_cadence": "semimonthly",
    }
    assert validate_answers("mutual-nda", {"effective_date": "1 October 2025"}) == {
        "effective_date": "1 October 2025"
    }


def test_validate_answers_rejects_bad_email_field() -> None:
    """Email fields must hold one address."""
    with pytest.raises(InvalidAnswersError) as excinfo:
        validate_answers("cookie-notice", {"contact_email": "ask our team"})
    assert excinfo.value.issues[0]["code"] == "invalid_email"


def test_answer_warnings_flag_placeholders_and_old_dates() -> None:
    """Accepted but doubtful answers come back as warnings."""
    warnings = service.answer_warnings(
        "mutual-nda",
        {
            "company_name": "[Employer Legal Name] GmbH - PLACEHOLDER",
            "effective_date": "2020-03-15",
        },
    )
    assert len(warnings) == 2
    assert "company_name looks like placeholder text" in warnings[0]
    assert "effective_date is March 15, 2020" in warnings[1]
    assert service.answer_warnings("mutual-nda", {"company_name": "Acme Inc."}) == []


def test_unfilled_fields_lists_remaining_placeholders() -> None:
    """Callers learn which fields are still placeholders."""
    assert unfilled_fields("one-way-nda", {"recipient_action": "evaluate a deal"}) == [
        "company_name",
        "permitted_use",
        "governing_state",
        "recipient_name",
    ]
    # The profiling description only matters once that branch is chosen.
    assert "profiling_description" not in unfilled_fields("privacy-policy-us", {})


def test_template_schema_publishes_max_length() -> None:
    """Agents can see each field's limit before generating."""
    fields = {field.key: field for field in get_template_fields("cookie-notice").fields}
    assert fields["company_name"].max_length == 300
    assert fields["include_app"].max_length is None


def test_api_generate_rejects_invalid_answers(simple_client: TestClient) -> None:
    """The JSON API returns a structured, fixable 422 and logs no answer text."""
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]),
        level="WARNING",
    )
    try:
        response = simple_client.post(
            "/api/templates/one-way-nda/generate",
            json={
                "answers": {"permitted_use": "secret clause " * 40, "law": "DE"},
                "format": "markdown",
            },
            headers={"User-Agent": "ClauseAITests/1.0"},
        )
    finally:
        logger.remove(sink)
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_answers"
    assert body["fields_url"] == "http://testserver/api/templates/one-way-nda"
    assert {issue["code"] for issue in body["issues"]} == {
        "too_long",
        "unknown_field",
    }
    assert "No document was generated" in body["detail"]
    log_text = "\n".join(messages)
    assert "ClauseAI generation rejected: slug=one-way-nda source=api" in log_text
    assert "permitted_use:too_long" in log_text
    assert "secret clause" not in log_text


def test_api_generate_json_reports_unfilled_and_warnings(
    simple_client: TestClient,
) -> None:
    """JSON responses say what is still a placeholder and what looks wrong."""
    response = simple_client.post(
        "/api/templates/mutual-nda/generate",
        json={
            "answers": {"company_name": "TBD"},
            "format": "markdown",
            "response": "json",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["unfilled_fields"] == [
        "company_entity",
        "effective_date",
        "governing_state",
        "other_party_name",
    ]
    assert "placeholder" in payload["warnings"][0]


def test_api_generate_error_messages_guide_a_retry(
    simple_client: TestClient,
) -> None:
    """Unknown slugs, formats, and emails say what would be accepted."""
    missing = simple_client.post("/api/templates/nda/generate", json={})
    assert missing.status_code == 404
    assert "Available slugs:" in missing.json()["detail"]

    bad_format = simple_client.post(
        "/api/templates/mutual-nda/generate", json={"format": "rtf"}
    )
    assert bad_format.status_code == 400
    assert "Use pdf, docx, odt, or markdown" in bad_format.json()["detail"]

    bad_email = simple_client.post(
        "/api/templates/mutual-nda/generate",
        json={"format": "markdown", "email": "nope"},
    )
    assert bad_email.status_code == 400
    assert "email is optional" in bad_email.json()["detail"]


@pytest.mark.asyncio
async def test_mcp_generate_errors_are_tool_errors() -> None:
    """MCP callers get the same fixable message as a tool error."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_generate_document(
            "one-way-nda", answers={"governing_law": "Germany"}, format="markdown"
        )
    assert "governing_law: not a field on this template" in str(excinfo.value)
    with pytest.raises(ToolError, match="Available slugs"):
        await mcp_generate_document("nope", format="markdown")


@pytest.mark.asyncio
async def test_mcp_generate_returns_unfilled_and_warnings() -> None:
    """The MCP result carries follow-ups for the model to relay."""
    with patch("clauseai.mcp.clauseai.record_generation", new=AsyncMock()):
        result = await mcp_generate_document(
            "mutual-nda",
            answers={"effective_date": "2020-01-05"},
            format="markdown",
        )
    assert result.answers == {"effective_date": "January 5, 2020"}
    assert "January 5, 2020" in result.markdown
    assert result.unfilled_fields == [
        "company_name",
        "company_entity",
        "governing_state",
        "other_party_name",
    ]
    assert "more than a year before today" in result.warnings[0]


@pytest.mark.asyncio
async def test_record_generation_logs_ip_and_user_agent() -> None:
    """Every generation log line identifies the caller."""
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]),
        level="INFO",
    )
    payload = generation_payload(
        slug="mutual-nda",
        fmt="markdown",
        answers={},
        email=None,
        source="api",
        ip_address="203.0.113.10",
        user_agent="ClauseAITests/1.0\n(injected)",
    )
    try:
        with patch("clauseai.notify.notify_generation", new_callable=AsyncMock):
            await record_generation(payload=payload)
            await asyncio.sleep(0)
    finally:
        logger.remove(sink)
    text = "\n".join(messages)
    assert "ip=203.0.113.10" in text
    assert "ua='ClauseAITests/1.0 (injected)'" in text


def test_api_feedback_logs_and_posts_to_slack(simple_client: TestClient) -> None:
    """Feedback is accepted, logged, and forwarded to the Slack notifier."""
    with patch(
        "clauseai.notify.notify_feedback",
        new_callable=AsyncMock,
        return_value=True,
    ) as notify:
        response = simple_client.post(
            "/api/feedback",
            json={
                "message": "The one-way NDA needs a governing law field.",
                "category": "missing_field",
                "slug": "one-way-nda",
                "email": "founder@example.com",
            },
            headers={
                "User-Agent": "ClauseAITests/1.0",
                "X-Forwarded-For": "203.0.113.9",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "received"
        # The Slack post runs in the background after the response.
        for _ in range(100):
            if notify.await_count:
                break
            time.sleep(0.01)
    notify.assert_awaited_once()
    payload = notify.await_args.args[0]
    assert payload["message"] == "The one-way NDA needs a governing law field."
    assert payload["category"] == "missing_field"
    assert payload["template_slug"] == "one-way-nda"
    assert payload["email"] == "founder@example.com"
    assert payload["source"] == "api"
    assert payload["ip_address"] == "203.0.113.9"


def test_api_feedback_errors_say_how_to_fix(simple_client: TestClient) -> None:
    """Rejected feedback explains what is accepted."""
    empty = simple_client.post("/api/feedback", json={})
    assert empty.status_code == 400
    assert "message is required" in empty.json()["detail"]

    category = simple_client.post(
        "/api/feedback", json={"message": "hi", "category": "praise"}
    )
    assert category.status_code == 400
    assert "bug, missing_field, template_request, other" in category.json()["detail"]

    slug = simple_client.post("/api/feedback", json={"message": "hi", "slug": "nda"})
    assert slug.status_code == 404
    assert "Available slugs:" in slug.json()["detail"]

    too_long = simple_client.post("/api/feedback", json={"message": "x" * 4001})
    assert too_long.status_code == 400
    assert "the limit is 4000" in too_long.json()["detail"]


def test_api_feedback_is_rate_limited(simple_client: TestClient) -> None:
    """One caller cannot flood the Slack channel."""
    with patch("clauseai.notify.notify_feedback", new_callable=AsyncMock) as notify:
        statuses = [
            simple_client.post("/api/feedback", json={"message": "hi"}).status_code
            for _ in range(service.FEEDBACK_RATE_LIMIT + 1)
        ]
    assert statuses == [200] * service.FEEDBACK_RATE_LIMIT + [429]
    limited = simple_client.post("/api/feedback", json={"message": "hi"})
    assert int(limited.headers["retry-after"]) > 0
    assert "do not retry in a loop" in limited.json()["detail"]


def test_feedback_rate_window_expires() -> None:
    """The limit resets once the window has passed."""
    for _ in range(service.FEEDBACK_RATE_LIMIT):
        service.check_feedback_rate("203.0.113.1", now=0.0)
    with pytest.raises(FeedbackRateLimitError):
        service.check_feedback_rate("203.0.113.1", now=1.0)
    service.check_feedback_rate("203.0.113.2", now=1.0)
    service.check_feedback_rate(
        "203.0.113.1", now=service.FEEDBACK_RATE_WINDOW_SECONDS + 1.0
    )


@pytest.mark.asyncio
async def test_mcp_send_feedback() -> None:
    """The MCP tool records feedback and rejects an empty message helpfully."""
    recorded: dict = {}

    async def _record(*, payload: dict) -> None:
        recorded["payload"] = payload

    with patch("clauseai.mcp.clauseai.record_feedback", new=_record):
        receipt = await send_feedback(
            "No template for a SAFE.", category="template_request"
        )
        with pytest.raises(ToolError, match="message is required"):
            await send_feedback("  ")
    assert receipt.status == "received"
    assert recorded["payload"]["source"] == "mcp"
    assert recorded["payload"]["category"] == "template_request"
    assert recorded["payload"]["template_slug"] is None


def test_feedback_slack_message_escapes_mentions() -> None:
    """Feedback text cannot ping the channel or forge links."""
    payload = service.feedback_payload(
        message="<!channel> broken <https://evil.test|click>",
        category="bug",
        slug="mutual-nda",
        email=None,
        source="mcp",
        ip_address="203.0.113.10",
        user_agent="ClauseAITests/1.0",
        mcp_client="claude-code",
        mcp_client_version="1.0.5",
    )
    message = build_feedback_slack_message(payload)
    fields = {
        field["title"]: field["value"] for field in message["attachments"][0]["fields"]
    }
    assert message["text"] == "ClauseAI feedback (bug): mutual-nda"
    assert fields["Message"].startswith("&lt;!channel&gt; broken")
    assert "<" not in fields["Message"]
    assert fields["Client"] == "claude-code 1.0.5"
    assert fields["Email"] == "not provided"


def test_feedback_payload_rejects_blank_message() -> None:
    """Service-level validation raises a typed error."""
    with pytest.raises(InvalidFeedbackError):
        service.feedback_payload(
            message="",
            category=None,
            slug=None,
            email=None,
            source="api",
            ip_address=None,
            user_agent=None,
        )
