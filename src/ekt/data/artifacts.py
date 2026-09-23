"""Checksummed immutable Parquet IO. Quantities remain Decimal, never float."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .errors import DataError


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported value: {type(value).__name__}")


def canonical_json(value) -> str:
    return json.dumps(value, default=json_default, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def checksum(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_payload(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def write_records(path, records: list[dict]) -> dict:
    """Atomically publish one immutable file, returning its complete reference.

    Reusing a path with identical records is safe; different contents is an error.
    No parent directory is created until all Arrow conversion has succeeded.
    """
    path = Path(path).expanduser().resolve()
    if records:
        # from_pylist uses keys from the first record only; union prevents data loss.
        keys = sorted({key for row in records for key in row})
        aligned = [{key: row.get(key) for key in keys} for row in records]
        table = pa.Table.from_pylist(aligned)
    else:
        table = pa.table({"_empty": pa.array([], type=pa.string())})
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".parquet-", dir=path.parent)
    os.close(handle)
    try:
        pq.write_table(table, temporary, compression="zstd", version="2.6")
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        file_hash = checksum(temporary)
        try:
            # A hard link is an atomic create-only publication on the same fs.
            os.link(temporary, path)
        except FileExistsError:
            if checksum(path) != file_hash:
                raise DataError("ARTIFACT_CONFLICT", "Immutable artifact already exists with different contents")
        return {"uri": str(path), "format": "parquet", "row_count": len(records), "checksum": file_hash}
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_table(ref: dict) -> list[dict]:
    """Read a complete artifact only after verifying checksum and row count."""
    if hasattr(ref, "model_dump"):
        ref = ref.model_dump(mode="python")
    if ref.get("format", "parquet") != "parquet":
        raise DataError("INVALID_ARTIFACT_FORMAT", "Only Parquet table references are supported")
    path = Path(ref["uri"])
    if not path.is_file():
        raise DataError("ARTIFACT_MISSING", "Referenced table does not exist")
    if not ref.get("checksum") or checksum(path) != ref["checksum"]:
        raise DataError("ARTIFACT_CHECKSUM_MISMATCH", "Referenced table checksum does not match")
    table = pq.read_table(path)
    if table.num_rows != ref.get("row_count"):
        raise DataError("ARTIFACT_ROW_COUNT_MISMATCH", "Referenced table row count does not match")
    return table.to_pylist()
