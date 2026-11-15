"""Multimodal SOP extraction (standalone images and scanned / image-containing PDF pages).

Converts visual SOP assets (temperature charts, escalation flowcharts, scanned pages, signage)
into structured Markdown so they flow through the same header-aware splitter, embedding model,
and Pinecone vector store as native text SOPs.

Extraction resolution order:
1. Custom extractor registered via ``set_multimodal_extractor`` (used for offline unit tests
   or custom local OCR pipelines).
2. Gemini Vision (``GEMINI_API_KEY`` / ``GEMINI_APKEY``, model ``GEMINI_MODEL`` default
   ``gemini-3.8-flash``).
3. OpenAI Vision (``OPENAI_API_KEY``, model ``OPENAI_VISION_MODEL`` default ``gpt-4o-mini``).
4. Local RapidOCR / Tesseract fallback if installed; otherwise returns ``""`` so that an
   unextractable image/scanned PDF fails cleanly with ``DocumentParseError`` (preserving F-01).
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Protocol

from rag.core.resilience import with_retry

logger = logging.getLogger(__name__)

VISION_TRANSCRIPTION_PROMPT = (
    "You are transcribing a Cold-Chain Logistics Standard Operating Procedure (SOP) visual asset "
    "for a compliance retrieval system.\n"
    "Convert all visible text, headings, tables, flowcharts, numeric temperature thresholds, "
    "SLA timers, and escalation rules into clean, well-structured GitHub-Flavored Markdown.\n"
    "- Use `#`, `##`, `###` headings for sections.\n"
    "- Preserve exact numbers, units (e.g., °C, km/h, hours), column names, and identifiers.\n"
    "- If the image is completely blank or contains no meaningful information, return an empty string."
)


class MultimodalExtractor(Protocol):
    def extract_image_bytes(self, image_bytes: bytes, mime_type: str, *, source_name: str = "") -> str: ...


_CUSTOM_EXTRACTOR: MultimodalExtractor | None = None


def set_multimodal_extractor(extractor: MultimodalExtractor | None) -> None:
    """Register or clear a custom multimodal extractor (e.g. for offline tests or custom OCR)."""
    global _CUSTOM_EXTRACTOR
    _CUSTOM_EXTRACTOR = extractor


def get_multimodal_extractor() -> MultimodalExtractor | None:
    return _CUSTOM_EXTRACTOR


def guess_image_mime_type(path_or_name: str | Path) -> str:
    ext = Path(path_or_name).suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    if ext in mapping:
        return mapping[ext]
    guessed, _ = mimetypes.guess_type(str(path_or_name))
    return guessed or "image/png"


class VisionLLMExtractor:
    """Cloud Vision LLM extractor supporting Gemini Vision and OpenAI Vision."""

    @with_retry(attempts=3, max_wait=5.0)
    def extract_image_bytes(self, image_bytes: bytes, mime_type: str, *, source_name: str = "") -> str:
        if not image_bytes:
            return ""

        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_APKEY")
        if gemini_key:
            return self._extract_with_gemini(image_bytes, mime_type, gemini_key)

        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            return self._extract_with_openai(image_bytes, mime_type, openai_key)

        return self._extract_with_local_ocr(image_bytes, source_name=source_name)

    @staticmethod
    def _extract_with_gemini(image_bytes: bytes, mime_type: str, api_key: str) -> str:
        from langchain_core.messages import HumanMessage
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        llm = ChatGoogleGenerativeAI(model=model_name, temperature=0, google_api_key=api_key)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        msg = HumanMessage(
            content=[
                {"type": "text", "text": VISION_TRANSCRIPTION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ]
        )
        response = llm.invoke([msg])
        return _normalise_llm_content(response.content)

    @staticmethod
    def _extract_with_openai(image_bytes: bytes, mime_type: str, api_key: str) -> str:
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI

        model_name = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini").strip()
        llm = ChatOpenAI(model=model_name, temperature=0, api_key=api_key)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        msg = HumanMessage(
            content=[
                {"type": "text", "text": VISION_TRANSCRIPTION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ]
        )
        response = llm.invoke([msg])
        return _normalise_llm_content(response.content)

    @staticmethod
    def _extract_with_local_ocr(image_bytes: bytes, *, source_name: str = "") -> str:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

            engine = RapidOCR()
            result, _ = engine(image_bytes)
            if result:
                return "\n".join(str(line[1]) for line in result if len(line) > 1 and line[1]).strip()
        except ImportError:
            pass
        logger.warning(
            "No multimodal Vision API key (GEMINI_API_KEY / OPENAI_API_KEY) or local OCR library available for %s",
            source_name or "image",
        )
        return ""


def _normalise_llm_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def extract_image_file(path: Path, *, extractor: MultimodalExtractor | None = None) -> str:
    """Transcribe a standalone image file (.png, .jpg, .webp, .tiff) into structured Markdown."""
    image_bytes = path.read_bytes()
    if not image_bytes.strip():
        return ""
    active = extractor or _CUSTOM_EXTRACTOR or VisionLLMExtractor()
    mime_type = guess_image_mime_type(path)
    return active.extract_image_bytes(image_bytes, mime_type, source_name=path.name).strip()


def extract_pdf_page_images(
    page: Any,
    *,
    source_name: str,
    page_number: int,
    extractor: MultimodalExtractor | None = None,
) -> str:
    """Extract and transcribe embedded images from a scanned/image-only PDF page."""
    active = extractor or _CUSTOM_EXTRACTOR or VisionLLMExtractor()
    parts: list[str] = []
    try:
        images = list(getattr(page, "images", None) or [])
    except Exception as exc:
        logger.warning("%s page %d: could not inspect embedded PDF images (%s)", source_name, page_number, exc)
        return ""

    for idx, img in enumerate(images):
        data = getattr(img, "data", b"")
        name = getattr(img, "name", f"page_{page_number}_img_{idx}.png")
        if not data:
            continue
        mime = guess_image_mime_type(name)
        text = active.extract_image_bytes(
            data,
            mime,
            source_name=f"{source_name}#page={page_number}",
        ).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)
