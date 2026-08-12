from __future__ import annotations

from pathlib import Path
import uuid

import pytest


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture()
def output_root(project_root: Path, request: pytest.FixtureRequest) -> Path:
    path = project_root / "outputs" / "tests" / request.node.name / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path
