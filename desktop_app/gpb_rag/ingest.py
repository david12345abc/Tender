from __future__ import annotations

import re
from pathlib import Path

from .normalize import light_ocr_fixes, normalize_whitespace, strip_repeated_headers_footers
from .schemas import FileMetadata

_PADDLE_OCR = None


def _get_paddle_ocr():
    global _PADDLE_OCR
    if _PADDLE_OCR is False:
        return None
    if _PADDLE_OCR is not None:
        return _PADDLE_OCR
    try:
        from paddleocr import PaddleOCR

        _PADDLE_OCR = PaddleOCR(lang="ru", show_log=False)
    except Exception:
        _PADDLE_OCR = False
    return _PADDLE_OCR if _PADDLE_OCR is not False else None


def _ocr_page_image(rgb_bytes: bytes, width: int, height: int) -> str:
    import numpy as np
    from PIL import Image

    img = np.array(Image.frombytes("RGB", (width, height), rgb_bytes))
    ocr = _get_paddle_ocr()
    if ocr is None:
        return ""
    try:
        result = ocr.ocr(img, cls=True)
    except Exception:
        return ""
    lines: list[str] = []
    if not result or result[0] is None:
        return ""
    for line in result[0]:
        if line and len(line) >= 2:
            txt = str(line[1][0] or "").strip()
            if txt:
                lines.append(txt)
    return "\n".join(lines)


def extract_pdf_pages(path: Path, *, ocr_if_scan: bool = True) -> tuple[list[str], FileMetadata]:
    pages: list[str] = []
    try:
        import fitz

        doc = fitz.open(path)
        n = min(doc.page_count, 120)
        for i in range(n):
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            text = normalize_whitespace(text)
            if ocr_if_scan and len(text) < 50:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    ocr_text = _ocr_page_image(pix.samples, pix.width, pix.height)
                    if len(ocr_text.strip()) > len(text.strip()):
                        text = normalize_whitespace(ocr_text)
                except Exception:
                    pass
            pages.append(light_ocr_fixes(text))
        meta = FileMetadata(file_name=path.name, file_type="pdf", pages=len(pages))
        return pages, meta
    except Exception:
        pass

    from ..document_text import _read_pdf

    blob = _read_pdf(path)
    if not blob.strip():
        meta = FileMetadata(file_name=path.name, file_type="pdf", pages=None)
        return [], meta
    paras = [p.strip() for p in blob.split("\n\n") if p.strip()]
    chunk_pages = max(1, len(blob) // 8000)
    step = max(1, len(paras) // chunk_pages)
    pseudo_pages: list[str] = []
    for i in range(0, len(paras), step):
        pseudo_pages.append("\n\n".join(paras[i : i + step]))
    meta = FileMetadata(file_name=path.name, file_type="pdf", pages=len(pseudo_pages) or 1)
    return pseudo_pages or [blob], meta


_SKIP_SUFFIXES = {
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".dll",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
}


def iter_analysis_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.is_dir():
        return files
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.startswith("~$"):
            continue
        suf = p.suffix.lower()
        if suf in _SKIP_SUFFIXES:
            continue
        files.append(p)
    return files[:300]


def ingest_directory(root: Path) -> tuple[list[tuple[FileMetadata, list[tuple[int | None, str]]]], list[str]]:
    """Возвращает список (метаданные файла, список (номер страницы или None, текст страницы/блока))."""
    from ..document_text import _extract_text_from_file

    items: list[tuple[FileMetadata, list[tuple[int | None, str]]]] = []
    notes: list[str] = []
    for path in iter_analysis_files(root):
        suf = path.suffix.lower()
        try:
            if suf == ".pdf":
                pages, meta = extract_pdf_pages(path)
                if not any(p.strip() for p in pages):
                    notes.append(f"{path.name}: PDF без извлекаемого текста")
                    items.append((meta, [(None, "")]))
                else:
                    numbered = [(i + 1, normalize_whitespace(strip_repeated_headers_footers(p))) for i, p in enumerate(pages)]
                    items.append((meta, numbered))
                continue

            text = _extract_text_from_file(path)
            text = normalize_whitespace(strip_repeated_headers_footers(text))
            text = light_ocr_fixes(text)
            meta = FileMetadata(file_name=path.name, file_type=suf.lstrip(".") or "file", pages=None)
            items.append((meta, [(None, text)]))
        except Exception as e:
            notes.append(f"{path.name}: ошибка чтения: {e}")
            items.append(
                (
                    FileMetadata(file_name=path.name, file_type=suf.lstrip(".") or "file", pages=None),
                    [(None, "")],
                )
            )
    return items, notes


def ingest_card_page_text(page_text: str, registry: str) -> tuple[FileMetadata, list[tuple[int | None, str]]]:
    clean = normalize_whitespace(strip_repeated_headers_footers(page_text))
    meta = FileMetadata(file_name=f"карточка_этп_{registry}.txt", file_type="card_html", pages=None)
    return meta, [(None, clean)]
