"""Точка входа для десктопного приложения поиска процедур секции Газпром."""
from __future__ import annotations


def _preload_rag_runtime() -> None:
    """Грузит PyTorch/RAG раньше PaddleOCR.

    На Windows Paddle может загрузить DLL так, что последующий импорт torch
    падает на torch/lib/shm.dll с WinError 127. RAG должен быть доступен всегда,
    поэтому фиксируем порядок загрузки нативных библиотек прямо на старте.
    """
    import faiss  # noqa: F401
    import torch  # noqa: F401
    import sentence_transformers  # noqa: F401


_preload_rag_runtime()

from desktop_app.app import main


if __name__ == "__main__":
    raise SystemExit(main())
