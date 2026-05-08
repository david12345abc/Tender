"""RAG-пайплайн для заполнения таблицы анализа карточки ЭТП ГПБ."""

from __future__ import annotations

from .pipeline import ragged_analysis_available, run_rag_table_analysis

__all__ = ["ragged_analysis_available", "run_rag_table_analysis"]
