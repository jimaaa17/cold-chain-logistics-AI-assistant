"""Format-specific parsers, registered by file extension.

Add a format by decorating a function with ``@register(".ext")``. Parsers raise on failure
(wrapped in ``DocumentParseError``) instead of returning ``[]``, so a broken file is reported
and retried rather than silently recorded as "ingested with zero chunks".
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from rag.core.models import Chunk

Parser = Callable[[Path, RecursiveCharacterTextSplitter], list[Chunk]]

_REGISTRY: dict[str, Parser] = {}
_MD_HEADERS = [("#", "Header_1"), ("##", "Header_2"), ("###", "Header_3")]


class DocumentParseError(Exception):
    pass


def register(*extensions: str) -> Callable[[Parser], Parser]:
    def decorator(fn: Parser) -> Parser:
        for ext in extensions:
            _REGISTRY[ext.lower()] = fn
        return fn

    return decorator


def supported_extensions() -> frozenset[str]:
    return frozenset(_REGISTRY)


def parse_document(path: Path, *, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    parser = _REGISTRY.get(path.suffix.lower())
    if parser is None:
        raise DocumentParseError(f"Unsupported file type: {path.suffix}")
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    try:
        chunks = parser(path, splitter)
    except DocumentParseError:
        raise
    except Exception as exc:  # normalise library-specific errors
        raise DocumentParseError(f"{path.name}: {type(exc).__name__}: {exc}") from exc

    cleaned = []
    for chunk in chunks:
        text = chunk.text.strip()
        if text:
            cleaned.append(Chunk(text=text, metadata=chunk.metadata))
    return cleaned


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")  # tolerate a UTF-8 BOM (common from Windows editors)


@register(".md", ".markdown")
def _parse_markdown(path: Path, splitter: RecursiveCharacterTextSplitter) -> list[Chunk]:
    sections = MarkdownHeaderTextSplitter(headers_to_split_on=_MD_HEADERS).split_text(_read_text(path))
    chunks = []
    for doc in splitter.split_documents(sections):
        headers = {key: doc.metadata[key] for _, key in _MD_HEADERS if doc.metadata.get(key)}
        breadcrumb = " > ".join(headers.values())
        # Contextual chunk header: the section path is embedded with the body, so a chunk like
        # "If ETA delay > 1 hour, divert..." stays retrievable for "cold-chain breach" queries.
        text = f"{breadcrumb}\n\n{doc.page_content}" if breadcrumb else doc.page_content
        chunks.append(Chunk(text=text, metadata={**headers, "section": breadcrumb} if breadcrumb else {}))
    return chunks


@register(".txt")
def _parse_text(path: Path, splitter: RecursiveCharacterTextSplitter) -> list[Chunk]:
    return [Chunk(text=t) for t in splitter.split_text(_read_text(path))]


@register(".pdf")
def _parse_pdf(path: Path, splitter: RecursiveCharacterTextSplitter) -> list[Chunk]:
    import pypdf

    reader = pypdf.PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise DocumentParseError(f"{path.name}: PDF is password-protected")
    chunks = []
    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        for text in splitter.split_text(page_text):
            chunks.append(Chunk(text=text, metadata={"page_number": page_number}))
    return chunks


@register(".csv", ".xlsx")
def _parse_table(path: Path, splitter: RecursiveCharacterTextSplitter) -> list[Chunk]:
    import pandas as pd

    # dtype=str keeps values exactly as written (no "1" -> "1.0" when a column has blanks).
    if path.suffix.lower() == ".csv":
        sheets = {None: pd.read_csv(path, dtype=str)}
    else:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)  # every sheet, not just the first

    chunks = []
    for sheet_name, frame in sheets.items():
        for row_index, row in enumerate(frame.to_dict(orient="records")):
            items = [f"{col}: {val}" for col, val in row.items() if pd.notna(val) and str(val).strip()]
            if not items:
                continue
            metadata: dict = {"row_index": row_index}
            if sheet_name is not None:
                metadata["sheet"] = str(sheet_name)
            chunks.append(Chunk(text=" | ".join(items), metadata=metadata))
    return chunks
