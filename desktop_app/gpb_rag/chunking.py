from __future__ import annotations

import hashlib
import re
from typing import Callable

from .schemas import ChunkPayload


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _split_long_paragraph(para: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    if len(para) <= chunk_chars:
        return [para] if para.strip() else []
    parts: list[str] = []
    start = 0
    while start < len(para):
        end = min(start + chunk_chars, len(para))
        chunk = para[start:end].strip()
        if chunk:
            parts.append(chunk)
        if end >= len(para):
            break
        start = max(0, end - overlap_chars)
    return parts


def semantic_recursive_chunks(
    text: str,
    *,
    chunk_size_tokens: int = 900,
    overlap_tokens: int = 200,
    file_name: str = "",
    page: int | None = None,
    section_hint: str | None = None,
    id_prefix: str = "",
    token_counter: Callable[[str], int] | None = None,
) -> list[ChunkPayload]:
    """Рекурсивное разбиение: блоки → абзацы → подстрочное деление с overlap."""
    counter = token_counter or approx_tokens
    chunk_chars = max(400, chunk_size_tokens * 3)
    overlap_chars = max(80, overlap_tokens * 3)

    blocks = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if counter(block) <= chunk_size_tokens:
            chunks.append(block)
            continue
        paras = [p.strip() for p in block.split("\n") if p.strip()]
        buf: list[str] = []
        buf_tokens = 0
        for p in paras:
            t = counter(p)
            if t > chunk_size_tokens:
                if buf:
                    chunks.append("\n".join(buf))
                    buf = []
                    buf_tokens = 0
                chunks.extend(_split_long_paragraph(p, chunk_chars, overlap_chars))
                continue
            if buf_tokens + t > chunk_size_tokens and buf:
                chunks.append("\n".join(buf))
                tail = "\n".join(buf[-2:]) if len(buf) >= 2 else ""
                buf = ([tail] if tail and counter(tail) <= overlap_tokens * 2 else [])
                buf_tokens = sum(counter(x) for x in buf)
            buf.append(p)
            buf_tokens += t
        if buf:
            chunks.append("\n".join(buf))

    out: list[ChunkPayload] = []
    for i, raw in enumerate(chunks):
        raw = raw.strip()
        if not raw:
            continue
        digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]
        cid = f"{id_prefix}chunk_{i}_{digest}"
        out.append(
            ChunkPayload(
                chunk_id=cid,
                file_name=file_name,
                page=page,
                section=section_hint,
                text=raw,
            )
        )
    return out
