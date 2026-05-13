from __future__ import annotations

from pathlib import Path

from ..lm_table_analysis import call_lm_studio_chat
from .chunking import semantic_recursive_chunks
from .embedding_store import FaissChunkIndex
from .ingest import ingest_directory
from .normalize import normalize_whitespace, strip_repeated_headers_footers
from .pipeline import CHUNK_TOKENS, DEFAULT_EMBED_MODEL, MAX_CHUNKS_PER_PROCEDURE, OVERLAP_TOKENS, TOP_K
from .schemas import ChunkPayload


def _format_chat_context(store: FaissChunkIndex, hits: list[tuple[int, float]]) -> str:
    blocks: list[str] = []
    for n, (idx, score) in enumerate(hits, start=1):
        if idx < 0 or idx >= len(store.chunks):
            continue
        ch = store.chunks[idx]
        loc = ch.file_name
        if ch.page is not None:
            loc += f", стр. {ch.page}"
        sec = f", раздел: {ch.section}" if ch.section else ""
        blocks.append(
            f"--- Фрагмент {n} (релевантность {score:.3f}; {loc}{sec}) ---\n{ch.text}"
        )
    return "\n\n".join(blocks)


def answer_question_from_saved_index(
    *,
    index_dir: Path,
    question: str,
    lm_base_url: str,
    lm_model: str,
    top_k: int | None = None,
    fallback_docs_dir: Path | None = None,
) -> str:
    question = question.strip()
    if not question:
        raise ValueError("Введите вопрос.")

    if (
        fallback_docs_dir is not None
        and not (index_dir / "index.faiss").is_file()
        and fallback_docs_dir.is_dir()
    ):
        build_chat_index_from_directory(index_dir=index_dir, docs_dir=fallback_docs_dir)

    store = FaissChunkIndex(DEFAULT_EMBED_MODEL)
    store.load(index_dir)
    hits = store.search(question, top_k=top_k or max(TOP_K, 7))
    if not hits:
        raise RuntimeError("Не удалось найти релевантные фрагменты в FAISS-индексе.")

    context = _format_chat_context(store, hits)
    system_prompt = (
        "Ты чат-бот аналитика закупочной документации. "
        "Отвечай только по предоставленным фрагментам карточки закупки и документов. "
        "Если ответа в контексте нет, прямо скажи, что в найденных фрагментах данных нет. "
        "Не придумывай факты. Отвечай по-русски, кратко и по делу. "
        "Если возможно, указывай источник: имя файла/страницу из заголовка фрагмента."
    )
    user_prompt = (
        f"Вопрос пользователя:\n{question}\n\n"
        "Найденные релевантные фрагменты:\n"
        f"{context}\n"
    )
    return call_lm_studio_chat(
        lm_base_url,
        lm_model,
        system_prompt,
        user_prompt,
        timeout_sec=900,
        max_tokens=1800,
    )


def build_chat_index_from_directory(*, index_dir: Path, docs_dir: Path) -> None:
    sources, _notes = ingest_directory(docs_dir)
    chunks: list[ChunkPayload] = []
    for meta, pages in sources:
        for page_no, page_txt in pages:
            txt = normalize_whitespace(strip_repeated_headers_footers(page_txt))
            if not txt.strip():
                continue
            chunks.extend(
                semantic_recursive_chunks(
                    txt,
                    chunk_size_tokens=CHUNK_TOKENS,
                    overlap_tokens=OVERLAP_TOKENS,
                    file_name=meta.file_name,
                    page=page_no,
                    id_prefix=meta.file_name[:48],
                )
            )
    chunks = chunks[:MAX_CHUNKS_PER_PROCEDURE]
    if not chunks:
        raise RuntimeError("В документах не найден текст для построения чат-индекса.")
    store = FaissChunkIndex(DEFAULT_EMBED_MODEL)
    store.fit(chunks)
    store.save(index_dir)
