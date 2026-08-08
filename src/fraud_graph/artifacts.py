from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fraud_graph.errors import ArtifactNotReadyError


@contextmanager
def atomic_artifact_directory(target: Path) -> Iterator[Path]:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ArtifactNotReadyError(f"artifact target already exists: {target}")

    temporary = parent / f".tmp-{target.name}-{uuid.uuid4().hex}"
    temporary.mkdir(parents=False, exist_ok=False)
    try:
        yield temporary
        temporary.rename(target)
    except Exception:
        _remove_owned_temporary_directory(temporary, parent)
        raise


def _remove_owned_temporary_directory(path: Path, parent: Path) -> None:
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    if resolved_path.parent != resolved_parent or not resolved_path.name.startswith(".tmp-"):
        raise RuntimeError(f"refusing to remove non-owned path: {path}")
    if resolved_path.exists():
        shutil.rmtree(resolved_path)
