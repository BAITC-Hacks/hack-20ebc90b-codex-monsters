"""Backend-local forecast smoke command; prints metadata, never source rows."""
import argparse
import json
from pathlib import Path

from ekt.data.artifacts import canonical_json
from ekt.data.boundary import as_payload
from . import build_forecast


def main():
    parser = argparse.ArgumentParser(description="Forecast a completed canonical snapshot")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--as-of", help="Timezone-aware replay timestamp; default snapshot cut-off")
    parser.add_argument("--sku", action="append", default=[])
    parser.add_argument("--warehouse", action="append", default=[])
    parser.add_argument("--horizon-days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result = as_payload(build_forecast(snapshot, {"run_id": "local-smoke", "as_of": args.as_of or snapshot["as_of"],
        "sku_ids": args.sku, "warehouse_ids": args.warehouse, "horizon_days": args.horizon_days,
        "seed": args.seed, "growth_overrides": [], "review_overrides": []}))
    print(canonical_json({"forecast_id": result["forecast_id"], "snapshot_id": result["snapshot_id"],
        "mode": result["mode"], "series_count": len(result["series"]), "horizon_days": args.horizon_days,
        "quality_status": result["quality"]["status"], "can_plan": result["quality"]["capabilities"]["can_plan"],
        "issue_codes": sorted({i["code"] for i in result["quality"]["issues"]}),
        "manifest_path": str(Path(result["corrected_demand_ref"]).parent / "forecast.json")}))


if __name__ == "__main__":
    main()
