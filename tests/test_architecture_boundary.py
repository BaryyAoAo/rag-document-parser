from __future__ import annotations

from pathlib import Path


FORBIDDEN_TERMS = (
    "ChunkBuilder",
    "PgsqlIngestRecord",
    "psycopg",
    "pgvector",
    "embedding_text",
    "rerank",
    "hybrid_search",
)


def test_parser_package_has_no_chunk_or_retrieval_dependencies(project_root: Path) -> None:
    source_root = project_root / "src" / "document_parser"
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for term in FORBIDDEN_TERMS:
            if term in text:
                violations.append(f"{path.relative_to(project_root)}: {term}")
    assert not violations, "\n".join(violations)
