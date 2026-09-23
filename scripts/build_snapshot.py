#!/usr/bin/env python3
"""Build an immutable local snapshot from explicit source and mapping manifests."""
import argparse
import json
from pathlib import Path

from ekt.data import build_snapshot_payload
from ekt.data.artifacts import canonical_json


def main():
    parser = argparse.ArgumentParser(description="Build a checksummed canonical snapshot. Source files are read-only.")
    parser.add_argument("source_manifest", nargs="?", help="Local JSON manifest with as_of, mode, sources and artifact_root")
    parser.add_argument("mapping_config", nargs="?", help="Local JSON mapping with version and source mappings")
    parser.add_argument("--systeme-root", help="Portable preset: directory containing extracted IEK and Systeme electric folders")
    parser.add_argument("--artifact-root", help="Local output directory (required with --systeme-root)")
    parser.add_argument("--as-of", help="Explicit RFC3339 context timestamp (required with --systeme-root)")
    parser.add_argument("--sku-limit", type=int, default=20, help="Number of observed piece SKUs in the real preview (default: 20)")
    args = parser.parse_args()
    if args.systeme_root:
        if args.source_manifest or args.mapping_config or not args.artifact_root or not args.as_of:
            parser.error("--systeme-root requires --artifact-root and --as-of, and cannot be combined with JSON positionals")
        from ekt.data.presets import systeme_preview_manifest
        manifest, mapping = systeme_preview_manifest(args.systeme_root, args.artifact_root, args.as_of, args.sku_limit)
    else:
        if not args.source_manifest or not args.mapping_config:
            parser.error("Supply source_manifest and mapping_config, or --systeme-root with --artifact-root and --as-of")
        with open(args.source_manifest, encoding="utf-8") as stream:
            manifest = json.load(stream)
        with open(args.mapping_config, encoding="utf-8") as stream:
            mapping = json.load(stream)
    result = build_snapshot_payload(manifest, mapping)
    # Safe summary: local paths and source rows stay in the local manifest.
    print(canonical_json({"snapshot_id": result["snapshot_id"], "mode": result["mode"],
                          "manifest_path": str(Path(next(iter(result["tables"].values()))["uri"]).parent / "manifest.json"),
                          "table_counts": {name: ref["row_count"] for name, ref in result["tables"].items()},
                          "quality_status": result["quality"]["status"],
                          "issue_codes": sorted({issue["code"] for issue in result["quality"]["issues"]})}))


if __name__ == "__main__":
    main()
