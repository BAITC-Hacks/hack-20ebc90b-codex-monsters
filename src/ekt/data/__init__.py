"""Participant B public boundary; shared runtime types remain owned by A."""

from .artifacts import read_table, write_records
from .errors import DataError
from .snapshot import build_snapshot_payload


def build_snapshot(source_manifest, mapping_config):
    from . import boundary
    try:
        source, mapping = boundary.snapshot_inputs(source_manifest, mapping_config)
        payload = build_snapshot_payload(source, mapping)
        return boundary.export_contract("SnapshotManifest", payload)
    except DataError as error:
        boundary.raise_domain(error.code, str(error), affected_sku_ids=error.affected_sku_ids, retryable=error.retryable)


__all__ = ["build_snapshot", "build_snapshot_payload", "read_table", "write_records"]
