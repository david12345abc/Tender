from __future__ import annotations

import os
import re
from pathlib import Path

from ..lm_table_analysis import ANALYSIS_JSON_KEYS
from .chunking import semantic_recursive_chunks
from .embedding_store import FaissChunkIndex
from .extraction import extract_fields_via_retrieval
from .ingest import ingest_card_page_text, ingest_directory
from .normalize import normalize_whitespace, strip_repeated_headers_footers
from .schemas import ChunkPayload

DEFAULT_EMBED_MODEL = os.environ.get(
    "GPB_RAG_EMBEDDING_MODEL",
    "intfloat/multilingual-e5-small",
)
MAX_CHUNKS_PER_PROCEDURE = int(os.environ.get("GPB_RAG_MAX_CHUNKS", "2400"))
TOP_K = int(os.environ.get("GPB_RAG_TOP_K", "5"))
CHUNK_TOKENS = int(os.environ.get("GPB_RAG_CHUNK_TOKENS", "950"))
OVERLAP_TOKENS = int(os.environ.get("GPB_RAG_CHUNK_OVERLAP", "200"))


def ragged_analysis_available() -> bool:
    if os.environ.get("GPB_ANALYSIS_LEGACY", "").strip().lower() in ("1", "true", "yes"):
        return False
    try:
        import faiss  # noqa: F401
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def run_rag_table_analysis(
    *,
    registry: str,
    page_text: str,
    unpacked_dir: Path,
    lm_base_url: str,
    lm_model: str,
    progress=None,
    stop_flag=None,
    debug_dir: Path | None = None,
    ingest_notes_out: list[str] | None = None,
) -> tuple[dict[str, str], str]:
    """Строит индекс по карточке и файлам в unpacked_dir, затем извлекает поля таблицы."""
    sources: list[tuple] = []
    card_meta = ingest_card_page_text(page_text, registry)
    sources.append(card_meta)

    dir_items, notes = ingest_directory(unpacked_dir)
    sources.extend(dir_items)
    if ingest_notes_out is not None:
        ingest_notes_out.extend(notes)

    chunks: list[ChunkPayload] = []
    for meta, pages in sources:
        if stop_flag and stop_flag():
            break
        for page_no, page_txt in pages:
            txt = normalize_whitespace(strip_repeated_headers_footers(page_txt))
            if not txt.strip():
                continue
            safe_prefix = re.sub(r"\W+", "_", meta.file_name)[:48]
            page_prefix = f"p{page_no}_" if page_no is not None else ""
            sub = semantic_recursive_chunks(
                txt,
                chunk_size_tokens=CHUNK_TOKENS,
                overlap_tokens=OVERLAP_TOKENS,
                file_name=meta.file_name,
                page=page_no,
                id_prefix=f"{safe_prefix}_{page_prefix}",
            )
            chunks.extend(sub)

    chunks = chunks[:MAX_CHUNKS_PER_PROCEDURE]
    if not chunks:
        empty = {k: "" for k in ANALYSIS_JSON_KEYS}
        return empty, "[RAG] Нет текста для индексации."

    store = FaissChunkIndex(DEFAULT_EMBED_MODEL)
    if progress:
        progress(f"RAG: embeddings ({DEFAULT_EMBED_MODEL}), чанков: {len(chunks)}…")
    store.fit(chunks)

    if debug_dir is not None:
        try:
            store.save(debug_dir)
        except Exception:
            pass

    parsed, raw_bundle = extract_fields_via_retrieval(
        store,
        lm_base_url=lm_base_url,
        lm_model=lm_model,
        top_k=TOP_K,
        timeout_sec=900,
        progress=progress,
        stop_flag=stop_flag,
    )
    return parsed, raw_bundle
