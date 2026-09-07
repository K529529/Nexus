"""Deterministic normalized UTF-8 line windows and candidate identities."""

from hashlib import sha256
from pathlib import PurePosixPath

from nexus.domain.context import CodeChunk, ContextCandidate


def normalize_text(content: str) -> str:
    return content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def text_hash(content: str) -> str:
    return sha256(normalize_text(content).encode("utf-8")).hexdigest()


def language_for(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".py": "python",
        ".java": "java",
        ".js": "javascript",
        ".ts": "typescript",
        ".md": "markdown",
        ".toml": "toml",
        ".json": "json",
        ".go": "go",
        ".rs": "rust",
        ".sql": "sql",
    }.get(suffix, suffix.lstrip(".") or "text")


class LineWindowChunker:
    def chunk(
        self,
        *,
        file_path: str,
        language: str,
        content: str,
        file_hash: str,
    ) -> tuple[CodeChunk, ...]:
        lines = normalize_text(content).splitlines(keepends=True)
        starts = (0,) if 0 < len(lines) <= 120 else range(0, len(lines), 100)
        return tuple(
            CodeChunk(
                file_path.replace("\\", "/"),
                language,
                None,
                start + 1,
                min(start + 120, len(lines)),
                "".join(lines[start : start + 120]),
                text_hash("".join(lines[start : start + 120])),
                file_hash,
            )
            for start in starts
        )


def identity(candidate: ContextCandidate) -> tuple[str, int, int, str]:
    chunk = candidate.chunk
    return chunk.file_path, chunk.start_line, chunk.end_line, chunk.content_hash


def estimated_tokens(content: str) -> int:
    return (len(content) + 3) // 4
