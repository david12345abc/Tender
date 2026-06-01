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
    cells: list[tuple[float, float, str]] = []
    if not result or result[0] is None:
        return ""
    for page in result or []:
        for line in page or []:
            if not line or len(line) < 2:
                continue
            try:
                box = line[0] or []
                xs = [float(point[0]) for point in box if len(point) >= 2]
                ys = [float(point[1]) for point in box if len(point) >= 2]
                txt = str(line[1][0] or "").strip()
            except Exception:
                txt = ""
                xs = []
                ys = []
            if txt:
                x_min = min(xs) if xs else 0.0
                y_mid = (min(ys) + max(ys)) / 2 if ys else 0.0
                cells.append((y_mid, x_min, txt))
    if not cells:
        return ""
    cells.sort(key=lambda item: (item[0], item[1]))
    rows: list[list[tuple[float, str]]] = []
    tolerance = max(10.0, height * 0.006)
    row_y: list[float] = []
    for y_mid, x_min, txt in cells:
        if not rows or abs(y_mid - row_y[-1]) > tolerance:
            rows.append([(x_min, txt)])
            row_y.append(y_mid)
        else:
            rows[-1].append((x_min, txt))
            row_y[-1] = (row_y[-1] + y_mid) / 2
    return "\n".join(" | ".join(txt for _x, txt in sorted(row)) for row in rows)


def _looks_like_poor_pdf_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 50:
        return True
    letters = sum(1 for ch in compact if ch.isalpha())
    digits = sum(1 for ch in compact if ch.isdigit())
    return letters < 40 and digits > letters * 2


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
            poor_text = _looks_like_poor_pdf_text(text)
            if ocr_if_scan and poor_text:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    ocr_text = _ocr_page_image(pix.samples, pix.width, pix.height)
                    if ocr_text.strip() and (poor_text or len(ocr_text.strip()) > len(text.strip())):
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
        if name.startswith("~$") or name.startswith("_") or name.startswith(".access"):
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


def ingest_card_page_text(
    page_text: str, registry: str, card_url: str = ""
) -> tuple[FileMetadata, list[tuple[int | None, str]]]:
    header_lines = [
        "=== Текст страницы карточки процедуры секции Газпром "
        "(снят из браузера: ожидание загрузки, прокрутка, обход вкладок) ===",
        f"Реестровый номер (из таблицы поиска): {registry}",
    ]
    url = (card_url or "").strip()
    if url:
        header_lines.append(f"URL карточки: {url}")
    header_lines.append(
        "Этот блок дополняется текстами из файлов документации в папке разархивированных документов."
    )
    combined = "\n".join(header_lines) + "\n\n" + (page_text or "")
    clean = normalize_whitespace(strip_repeated_headers_footers(combined))
    meta = FileMetadata(file_name=f"карточка_этп_{registry}.txt", file_type="card_html", pages=None)
    return meta, [(None, clean)]
