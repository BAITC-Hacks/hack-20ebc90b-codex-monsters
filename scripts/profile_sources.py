#!/usr/bin/env python3
"""Read-only source profiling: PYTHONPATH=src python scripts/profile_sources.py --help."""
import argparse
import json

from ekt.data.artifacts import canonical_json
from ekt.data.sources import profile_source


def main():
    parser = argparse.ArgumentParser(description="Profile registered local source headers, date range, UOM and quantity signs. Never prints commercial rows.")
    parser.add_argument("source_manifest", nargs="?", help="Local JSON manifest")
    parser.add_argument("mapping_config", nargs="?", help="Local JSON mapping")
    parser.add_argument("--systeme-root", help="Directory containing extracted IEK and Systeme electric folders")
    parser.add_argument("--as-of", help="Explicit RFC3339 context timestamp for portable preset")
    args = parser.parse_args()
    if args.systeme_root:
        if not args.as_of or args.source_manifest or args.mapping_config:
            parser.error("--systeme-root requires --as-of and cannot be combined with JSON positionals")
        from ekt.data.presets import systeme_preview_manifest
        manifest, mapping = systeme_preview_manifest(args.systeme_root, "/unused-profile-only", args.as_of)
    else:
        if not args.source_manifest or not args.mapping_config:
            parser.error("Supply two JSON manifests, or --systeme-root with --as-of")
        with open(args.source_manifest, encoding="utf-8") as stream:
            manifest = json.load(stream)
        with open(args.mapping_config, encoding="utf-8") as stream:
            mapping = json.load(stream)
    profiles = [profile_source(source, mapping.get("sources", {}).get(source["source_id"], {})) for source in manifest["sources"]]
    print(canonical_json(profiles))


if __name__ == "__main__":
    main()
