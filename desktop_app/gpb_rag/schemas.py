from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileMetadata:
    file_name: str
    file_type: str
    pages: int | None = None


@dataclass
class ChunkPayload:
    chunk_id: str
    file_name: str
    text: str
    page: int | None = None
    section: str | None = None
