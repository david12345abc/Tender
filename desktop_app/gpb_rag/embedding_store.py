from __future__ import annotations

import json
from pathlib import Path

from .schemas import ChunkPayload


class FaissChunkIndex:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None
        self.index = None
        self.chunks: list[ChunkPayload] = []

    def _encode_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def fit(self, chunks: list[ChunkPayload]) -> None:
        import faiss
        import numpy as np

        self.chunks = chunks
        if not chunks:
            raise ValueError("Нет чанков для индексации.")
        model = self._encode_model()
        texts = [f"passage: {c.text}" for c in chunks]
        emb = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=16,
        )
        mat = np.asarray(emb, dtype=np.float32)
        dim = mat.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(mat)
        self.index = index

    def search(self, query: str, top_k: int = 5) -> list[tuple[int, float]]:
        import numpy as np

        if self.index is None or not self.chunks:
            return []
        model = self._encode_model()
        q = model.encode(
            [f"query: {query}"],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        qv = np.asarray(q, dtype=np.float32)
        scores, idxs = self.index.search(qv, min(top_k, len(self.chunks)))
        out: list[tuple[int, float]] = []
        for i, s in zip(idxs[0].tolist(), scores[0].tolist()):
            if i < 0:
                continue
            out.append((int(i), float(s)))
        return out

    def save(self, directory: Path) -> None:
        import faiss

        directory.mkdir(parents=True, exist_ok=True)
        if self.index is None:
            return
        index_path = directory / "index.faiss"
        try:
            faiss.write_index(self.index, str(index_path))
        except Exception:
            serialized = faiss.serialize_index(self.index)
            index_path.write_bytes(bytes(serialized))
        meta = [
            {
                "chunk_id": c.chunk_id,
                "file_name": c.file_name,
                "page": c.page,
                "section": c.section,
                "text": c.text[:8000],
            }
            for c in self.chunks
        ]
        (directory / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, directory: Path) -> None:
        import faiss
        import numpy as np

        index_path = directory / "index.faiss"
        meta_path = directory / "metadata.json"
        if not index_path.is_file() or not meta_path.is_file():
            raise FileNotFoundError(f"FAISS-индекс не найден в папке: {directory}")

        try:
            self.index = faiss.read_index(str(index_path))
        except Exception:
            raw_index = np.frombuffer(index_path.read_bytes(), dtype="uint8")
            self.index = faiss.deserialize_index(raw_index)
        raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        chunks: list[ChunkPayload] = []
        for item in raw_meta:
            if not isinstance(item, dict):
                continue
            chunks.append(
                ChunkPayload(
                    chunk_id=str(item.get("chunk_id") or ""),
                    file_name=str(item.get("file_name") or ""),
                    text=str(item.get("text") or ""),
                    page=item.get("page"),
                    section=item.get("section"),
                )
            )
        self.chunks = chunks
