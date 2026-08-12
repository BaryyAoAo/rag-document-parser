from __future__ import annotations

from pathlib import Path


FILE_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pptx": "pptx",
}


def detect_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in FILE_TYPE_BY_SUFFIX:
        raise ValueError(f"Unsupported file type: {suffix or '<no suffix>'}")
    return FILE_TYPE_BY_SUFFIX[suffix]
