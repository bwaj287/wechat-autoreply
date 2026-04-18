from __future__ import annotations

import hashlib
from pathlib import Path

import fitz
from pypdf import PdfReader

from erge_gateway.config import Settings
from erge_gateway.schemas import IngestResult


def _render_pdf_pages(path: Path, settings: Settings) -> list[str]:
    stat = path.stat()
    digest = hashlib.sha256(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")).hexdigest()[:16]
    output_dir = settings.tmp_root / "pdf-pages" / digest
    output_dir.mkdir(parents=True, exist_ok=True)

    rendered_paths: list[str] = []
    document = fitz.open(path)
    try:
        for index, page in enumerate(document, start=1):
            output_path = output_dir / f"page-{index:03d}.png"
            if not output_path.exists():
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                pixmap.save(output_path)
            rendered_paths.append(str(output_path))
    finally:
        document.close()
    return rendered_paths


def ingest_pdf(path: Path, settings: Settings) -> IngestResult:
    reader = PdfReader(str(path))
    text_chunks: list[str] = []
    total_chars = 0
    max_chunk_chars = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            text_chunks.append(text)
            text_len = len(text)
            total_chars += text_len
            max_chunk_chars = max(max_chunk_chars, text_len)
    page_count = len(reader.pages)
    avg_chars = int(total_chars / page_count) if page_count else 0
    extraction_mode = "text_native"
    routing_hint = None
    image_paths: list[str] = []
    should_treat_as_scan = (
        total_chars == 0
        or (
            total_chars < settings.pdf_text_threshold_chars
            and avg_chars < settings.pdf_avg_chars_per_page_threshold
            and max_chunk_chars < 40
        )
    )
    if should_treat_as_scan:
        extraction_mode = "ocr_scan"
        routing_hint = "scan_fallback_ready"
        image_paths = _render_pdf_pages(path, settings)
    return IngestResult(
        kind="pdf",
        source_path=str(path),
        text_chunks=text_chunks,
        image_paths=image_paths,
        metadata={
            "page_count": page_count,
            "total_chars": total_chars,
            "avg_chars_per_page": avg_chars,
            "max_chunk_chars": max_chunk_chars,
            "extraction_mode": extraction_mode,
            "ocr_used": extraction_mode == "ocr_scan",
            "rendered_page_count": len(image_paths),
        },
        routing_hint=routing_hint,
    )
