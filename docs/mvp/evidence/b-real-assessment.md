# Data preview evidence

Snapshot: `b5bdede6b9158d4ba836f7d79b2fb99e3cf049a5565a8dcd7e89ef0d4e482ad1`
Cut-off: 2026-09-22T23:59:59+05:00; mode: `real_preview`; quality: `degraded`.

| Measure | Value |
|---|---:|
| Selected / source SKUs | 20 / 565 |
| SKU coverage | 3.54% |
| Accepted / source keyed rows | 35912 / 77312 |
| Accepted row coverage | 46.45% |
| Can plan / approve / export | False / False / False |

## Sources

| Source | Role | Status | SHA-256 |
|---|---|---|---|
| S08-master | sku_master | mapped | `891b16288210a044c624141809f8c7d6ff8f4183ec29a5e5bf134981eb4f223e` |
| S08 | sales_events | mapped | `5ca3997c13f1f65d73db5141122b92deab64f1029bf9b43fd3dd7ade65151128` |
| S07 | supplier_terms | mapped | `4fc809ecf9c0a73d97f13f7a16e7dbb20425ab97a6b2d9a0f6be0bbe1a326a7b` |
| S12 | inventory_snapshots | metadata_only | `17aa50b7697aba5598997b1b4bba9de3950169bc81e97fd10c4d8b0348bd9a3d` |
| S01 | supplier_terms | metadata_only | `4705b5bbbaa7c55541a151f146270bcbdc72a4ce28686fdb3378575733c4a446` |

## S08: full source denominator

Keyed rows: 77312; non-keyed rows: 1; distinct SKUs: 565.
Source dates: 2023-01-18 to 2026-09-22 (not a completeness assertion).
Keyed quantity signs: `{"missing": 13, "negative": 302, "positive": 76997}`.
Non-keyed quantity signs: `{"positive": 1}`.
Selected keyed rows before validation: 35921.

## Blocking reasons

| Code | Issue count |
|---|---:|
| MISSING_CURRENT_STOCK | 20 |
| MISSING_PURCHASE_MAPPING | 60 |
| MISSING_QUANTITY | 1 |
| MISSING_SUPPLIER_TERMS | 60 |
| UNSUPPORTED_SIGNED_MOVEMENT | 8 |

Issue counts can exceed SKU counts because one SKU can lack several fields.

## Measured elapsed time

Single local run; filesystem cache uncontrolled. No SLA or scale guarantee. Forecast not timed.

Python 3.12.1; Darwin arm64.

- preset_selection: 2.969 s
- full_source_profile: 2.994 s
- snapshot_build: 3.821 s
- total: 9.793 s

## Limitations

- Real preview is not a complete ERP reconciliation; source coverage and movement semantics require confirmation.
- Full-source signs count keyed rows separately from summaries; profile_sources may include a non-SKU summary quantity.
- Full-source denominators include all rows in the supplied export, not just dates before the cutoff; selected keyed rows precede semantic validation.
- Subset selection favors frequently observed SKUs and is not representative of the long tail.
- Metadata-only sources are registered, not imported as current stock or full supplier demand.
- Missing dates are unknown, not observed zero demand. The preview is not eligible for ordering without verified inputs.
- No real accuracy, recovered lost-demand truth, achieved service level, savings or time reduction is measured here.
