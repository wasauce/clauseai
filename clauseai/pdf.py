"""Convert filled markdown to PDF with md2pdf / WeasyPrint."""

from __future__ import annotations

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from clauseai.log import get_logger

logger = get_logger(__name__)

try:
    from md2pdf.core import md2pdf

    MD2PDF_AVAILABLE = True
    MD2PDF_ERROR: str | None = None
except ImportError as exc:
    md2pdf = None  # type: ignore[assignment]
    MD2PDF_AVAILABLE = False
    MD2PDF_ERROR = str(exc)


class PDFGenerationError(Exception):
    """Raised when PDF generation fails."""


class PDFDependencyError(ImportError):
    """Raised when md2pdf or WeasyPrint is missing."""


def check_pdf_dependencies() -> None:
    """Raise if PDF generation dependencies are unavailable."""
    if not MD2PDF_AVAILABLE:
        raise PDFDependencyError(
            "PDF generation is not available. md2pdf and its "
            "dependencies are required. Install with: pip install md2pdf\n"
            f"Original error: {MD2PDF_ERROR}"
        )


def get_default_pdf_css() -> str:
    """Return embedded CSS for generated legal PDFs."""
    return """@page {
    size: letter;
    margin: 1in 0.75in 1in 0.75in;
    @bottom-center {
        content: counter(page);
        font-family: 'Times New Roman', serif;
        font-size: 11pt;
    }
}

body {
    font-family: 'Times New Roman', serif;
    font-size: 12pt;
    line-height: 1.5;
    color: #000;
    text-align: justify;
    margin: 0;
    padding: 0;
}

p {
    text-align: justify !important;
    margin: 0 0 12pt 0;
}

h1, h2, h3, h4, h5, h6 {
    font-family: 'Times New Roman', serif;
    font-weight: bold;
    color: #000;
    text-align: left;
    page-break-after: avoid;
    margin: 18pt 0 12pt 0;
}

h1 {
    font-size: 16pt;
    text-align: center;
    text-transform: uppercase;
    margin: 24pt 0 18pt 0;
}

h2 {
    font-size: 14pt;
    text-transform: uppercase;
    margin: 20pt 0 14pt 0;
}

ul, ol {
    margin: 0 0 12pt 0;
    padding-left: 24pt;
}

li {
    margin-bottom: 6pt;
}

table {
    border-collapse: collapse;
    width: 100%;
    margin: 12pt 0;
    font-size: 11pt;
}

th, td {
    border: 1px solid #000;
    padding: 6pt 8pt;
    text-align: left;
    vertical-align: top;
}

th {
    font-weight: bold;
    background-color: #f5f5f5;
}

hr {
    border: none;
    border-top: 1pt solid #000;
    margin: 24pt 0;
}

a {
    color: #000;
    text-decoration: underline;
}

p, li, th, td {
    orphans: 2;
    widows: 2;
}
"""


async def markdown_to_pdf(
    markdown_content: str,
    css_file_path: Optional[str] = None,
    base_url: Optional[str] = None,
) -> bytes:
    """Convert markdown to PDF bytes."""
    check_pdf_dependencies()
    if not markdown_content or not markdown_content.strip():
        raise ValueError("Markdown content cannot be empty")

    if css_file_path is None:
        css_content = get_default_pdf_css()
    else:
        try:
            with open(css_file_path, "r", encoding="utf-8") as handle:
                css_content = handle.read()
        except OSError:
            logger.warning(f"CSS file not found at {css_file_path}; using default")
            css_content = get_default_pdf_css()

    executor = ThreadPoolExecutor(max_workers=2)
    loop = asyncio.get_running_loop()

    try:

        def _generate_pdf() -> bytes:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as pdf_file:
                pdf_file_path = pdf_file.name
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".css", delete=False, encoding="utf-8"
            ) as css_file:
                css_temp_path = css_file.name
                css_file.write(css_content)
            try:
                md2pdf(
                    pdf_file_path,
                    raw=markdown_content,
                    css=css_temp_path,
                    base_url=base_url,
                )
                with open(pdf_file_path, "rb") as handle:
                    return handle.read()
            finally:
                for path in (pdf_file_path, css_temp_path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        pdf_bytes = await loop.run_in_executor(executor, _generate_pdf)
        logger.info(
            f"Generated PDF from markdown "
            f"({len(markdown_content)} chars -> {len(pdf_bytes)} bytes)"
        )
        return pdf_bytes
    except PDFDependencyError:
        raise
    except Exception as exc:
        logger.error(f"Failed to generate PDF from markdown: {exc}")
        raise PDFGenerationError(f"PDF generation failed: {exc}") from exc
    finally:
        executor.shutdown(wait=False)
