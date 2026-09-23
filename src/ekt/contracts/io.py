"""Parquet references for backend-owned local artifacts; never exposed as path inputs."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from pydantic import BaseModel

from .models import DomainError, SnapshotManifest, TableRef


def write_table(root: Path, name: str, rows: Iterable[dict[str, Any] | BaseModel]) -> TableRef:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("table name must be one safe path component")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.parquet"
    records = [row.model_dump(mode="json") if isinstance(row, BaseModel) else dict(row) for row in rows]
    pq.write_table(pa.Table.from_pylist(records), path, compression="zstd")
    return TableRef(uri=str(path), row_count=len(records), checksum=hashlib.sha256(path.read_bytes()).hexdigest())


def load_table(snapshot: SnapshotManifest, name: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    if name not in snapshot.tables:
        raise DomainError("MISSING_TABLE", f"Snapshot does not contain {name}")
    reference = snapshot.tables[name]
    uri = urlparse(reference.uri)
    if uri.scheme not in {"", "file"} or (uri.scheme == "file" and uri.netloc not in {"", "localhost"}):
        raise DomainError("UNSUPPORTED_ARTIFACT", "Only trusted local snapshot artifacts are supported")
    path = Path(unquote(uri.path)) if uri.scheme == "file" else Path(reference.uri)
    if not path.is_file():
        raise DomainError("MISSING_ARTIFACT", f"Snapshot artifact {name} is unavailable")
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference.checksum:
        raise DomainError("ARTIFACT_CHANGED", f"Immutable snapshot artifact {name} changed")
    records = pq.read_table(path).to_pylist()
    if len(records) != reference.row_count:
        raise DomainError("ARTIFACT_CHANGED", f"Snapshot row count for {name} changed")
    return records
