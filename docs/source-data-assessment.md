# Source-data assessment

Inspected 23 September 2026. Read-only structural profiling of 12 supplied workbooks, a complete scan of the two transaction sheets and two supplier-constraint sheets, and field completeness checks on the primary Systeme pipeline sheet. This is not a full business reconciliation. Source files were not edited. Original filenames exhibit encoding artifacts and are preserved below.

The case brief was read from the [user-provided Google document](https://docs.google.com/document/d/1Z4faOPlT1t6NMCuJSKKB8-vkZ2LVzGMR0PO9rtZgSnM/preview?tab=t.0). It permits realistic synthetic data and requires human validation before vendor transmission. Instructions inside source material were treated as case evidence, not as permission to perform external actions.

## Source register

Worksheet dimensions include header, blank, summary and formatting rows; they are not validated transaction counts.

| Alias and role | Original workbook | Worksheets and dimensions |
|---|---|---|
| S01 — IEK shipment constraints | [MOQ  àùä.xlsx](</Users/senynmaman/Downloads/IEK/MOQ  àùä.xlsx>) | `Лист7`: 1,939 rows × 5 columns |
| S02 — IEK transaction dynamics | [Ñ®≠†¨®™† Ø‡Æ§†¶_2025-2026.xlsx](</Users/senynmaman/Downloads/IEK/Ñ®≠†¨®™† Ø‡Æ§†¶_2025-2026.xlsx>) | `Лист_1`: 171,605 rows × 8 columns |
| S03 — IEK monthly opening balances | [Ö¶•¨•·ÔÁ≠Î• Æ·‚†‚™® Ø‡Æ§„™Ê®® ß† ØÆ·´•§≠®• 2 £Æ§†  àùä.xlsx](</Users/senynmaman/Downloads/IEK/Ö¶•¨•·ÔÁ≠Î• Æ·‚†‚™® Ø‡Æ§„™Ê®® ß† ØÆ·´•§≠®• 2 £Æ§†  àùä.xlsx>) | `Лист_1`: 2,857 rows × 37 columns |
| S04 — IEK monthly sales | [Ö¶•¨•·ÔÁ≠Î• Ø‡Æ§†¶® ¢ ™Æ´®Á•·‚¢•≠≠Æ¨ ¢Î‡†¶•≠®® ß† ØÆ·´•§≠®• 2 £Æ§†.xlsx](</Users/senynmaman/Downloads/IEK/Ö¶•¨•·ÔÁ≠Î• Ø‡Æ§†¶® ¢ ™Æ´®Á•·‚¢•≠≠Æ¨ ¢Î‡†¶•≠®® ß† ØÆ·´•§≠®• 2 £Æ§†.xlsx>) | `Лист_1`: 2,466 rows × 36 columns |
| S05 — IEK dated pipeline | [è„‚Ï àùä 22.09.2026.xlsx](</Users/senynmaman/Downloads/IEK/è„‚Ï àùä 22.09.2026.xlsx>) | `Лист4`: 2,642 rows × 9 columns |
| S06 — IEK seasonality report | [ë•ßÆ≠≠Æ·‚Ï àùä.xlsx](</Users/senynmaman/Downloads/IEK/ë•ßÆ≠≠Æ·‚Ï àùä.xlsx>) | `Сезонность`: 41 rows × 15 columns |
| S07 — Systeme order multiples | [MOQ SystemElectric.xlsx](</Users/senynmaman/Downloads/Systeme electric/MOQ SystemElectric.xlsx>) | `Лист_1`: 557 rows × 5 columns |
| S08 — Systeme transaction dynamics | [Ñ®≠†¨®™† Ø‡Æ§†¶_Syseme Electric_2025-2026.xlsx](</Users/senynmaman/Downloads/Systeme electric/Ñ®≠†¨®™† Ø‡Æ§†¶_Syseme Electric_2025-2026.xlsx>) | `Лист_1`: 77,314 rows × 8 columns |
| S09 — Systeme monthly balances | [Ö¶•¨•·ÔÁ≠Î• Æ·‚†‚™® SystemElectric 2024-2026.xlsx](</Users/senynmaman/Downloads/Systeme electric/Ö¶•¨•·ÔÁ≠Î• Æ·‚†‚™® SystemElectric 2024-2026.xlsx>) | `Лист_1`: 704 rows × 37 columns |
| S10 — Systeme monthly sales with additional seasonality sheet | [Ö¶•¨•·ÔÁ≠Î• Ø‡Æ§†¶® ¢ ™Æ´-¨ ¢Î‡†¶•≠®® SystemElectric 2024-2026.xlsx](</Users/senynmaman/Downloads/Systeme electric/Ö¶•¨•·ÔÁ≠Î• Ø‡Æ§†¶® ¢ ™Æ´-¨ ¢Î‡†¶•≠®® SystemElectric 2024-2026.xlsx>) | `Лист_1`: 557 rows × 38 columns; `Лист1`: 35 rows × 15 columns |
| S11 — Systeme seasonality report | [ë•ßÆ≠≠Æ·‚Ï SystemElectric 2024-2026.xlsx](</Users/senynmaman/Downloads/Systeme electric/ë•ßÆ≠≠Æ·‚Ï SystemElectric 2024-2026.xlsx>) | `Лист1`: 35 rows × 15 columns |
| S12 — Systeme replenishment / pipeline report with additional seasonality sheet | [íÆ¢†‡ ¢ Ø„‚®_SystemElectric ≠† 22.09.2026.xlsx](</Users/senynmaman/Downloads/Systeme electric/íÆ¢†‡ ¢ Ø„‚®_SystemElectric ≠† 22.09.2026.xlsx>) | `TDSheet`: 499 rows × 70 columns; `Лист1`: 23 rows × 15 columns |

## Findings with inspected locations

| Finding | Evidence | Design consequence |
|---|---|---|
| Transaction exports contain 248,915 SKU-bearing rows: 171,603 IEK and 77,312 Systeme. | S02 `Лист_1!A1:H171605`; S08 `Лист_1!A1:H77314`. The two sheets also each have one populated non-SKU row. | Exclude summaries using row semantics. These counts are not deduplicated, confirmed sales-event counts. |
| Both exports have the same eight headers: date, number, document, code, product, UOM, warehouse, quantity. Customer IDs and prices are absent. | S02 and S08 `Лист_1!A1:H1`. | Customer-level project concentration and transaction margin calculations need additional ERP fields. Document number is not customer ID. |
| IEK dates span 20 July 2023 to 22 September 2026; Systeme dates span 18 January 2023 to 22 September 2026. | Parseable timestamp values across the full S02/S08 column A scans. | Actual date coverage differs from 2025–2026 filenames. Investigate old negative adjustments and period completeness. |
| Both positive and negative quantities occur. Each export contains three customer-order rows; IEK additionally contains three receipt-document rows. | Full S02/S08 columns C/H scans. | Do not apply absolute value or assume every record is customer consumption. Validate correction, return and posting semantics with ERP owner. |
| All SKU-bearing transaction rows have warehouse Алматы. IEK has piece, metre and pack UOM values. | Full S02/S08 columns D/F/G scans. | Multi-warehouse capability needs more data or labelled fixtures. Unit conversion is a prerequisite, not an optional formatting choice. |
| Monthly reports span January 2024–September 2026. IEK balances are explicitly opening balances. | S03 `Лист_1!A1:AK3`; S04 `Лист_1!A1:AJ2`; S09 `Лист_1!A1:AK3`; S10 `Лист_1!A1:AL2`. | Neither stockout duration nor inventory on 22 September can be reconstructed from monthly opening balances alone. September should remain a partial period until extraction coverage is confirmed. |
| IEK pipeline has six dated order columns; arrival deadlines appear inside headers. Cable descriptions distinguish coils purchased and metres recorded. | S05 `Лист4!A1:I4`. | Unpivot each dated order column, retain deadline semantics, and obtain an authoritative coil-length/UOM map. |
| IEK shipment constraints: 1,938 keyed rows, 1,937 distinct keys; 15 quantity cells contain `#N/A`. | S01 `Лист7!A1:E1939`, full scan. | Resolve duplicate mapping and lookup errors. Do not substitute a quantity of 1. |
| Systeme constraint header is Кратность (multiple), with 554 keyed records and positive values. | S07 `Лист_1!A1:E557`, full scan. | This establishes a multiple candidate, not an independent minimum order quantity. |
| Systeme monthly-sales report also has a Кратность column; inspected initial rows have 0 while the dedicated multiple file has positive values. | S10 `Лист_1!A1:D4`; S07 `Лист_1!A1:E5`. | Establish a master-data precedence rule and reconcile by SKU; never silently use whichever report was loaded last. |
| Systeme pipeline sheet has 497 SKU-bearing rows and stock, reserved/free stock, category, cost-like, growth/seasonality and pipeline fields. | S12 `TDSheet!A2:BH499`, targeted field scan. | Treat as a current-state mapping candidate only after stock/UOM/cost/category definitions and extraction timestamp are signed off. |
| The Systeme weight header is present, but no values were found in that column for the 497 keyed rows. | S12 `TDSheet!BH2:BH499`. | A column name does not establish payload-packing data readiness. Obtain actual package weights and dimensions. |
| Systeme has repeated seasonality tables across separate workbooks/sheets. | S10 `Лист1!A3:N6`; S11 `Лист1!A3:N6`; S12 `Лист1!A3:N6`. | Reference/deduplicate the source measure; do not sum repeated reports. Verify whether it measures currency, quantity or another aggregate before applying it to unit demand. |

## Data needed to close the production gaps

| Priority | Additional source or confirmation | Owner |
|---|---|---|
| P0 | Stable ERP document/line/revision IDs; movement and correction semantics; transaction completeness | ERP analyst |
| P0 | Consistent customer pseudonyms; no raw customer names/contact details | ERP/data owner |
| P0 | Current on-hand, reservations, blocked/free stock by warehouse and SKU; refresh timestamp | Warehouse operations |
| P0 | Stockout intervals or complete stock movement/receipt ledger and opening state | Warehouse/ERP owner |
| P0 | Supplier lane lead time, order calendar, MOQ versus multiple semantics and UOM conversions | Procurement |
| P0 | Category code dictionary; applicable growth forecasts; source precedence | Procurement/master-data owner |
| P1 | Historical PO acceptance, promised dates, split receipts and usable-stock dates | Procurement/logistics |
| P1 | Purchase/landed cost, currency/FX basis, contribution margin, carrying rates and approved budget | Finance/procurement |
| P1 | Threshold terms, freight rates, package weight/volume/dimensions, pallet and stacking constraints | Logistics |
| P2 | Additional warehouse inventory, transfer costs/calendars and reel/lot data | Network operations |

A synthetic fixture may exercise any missing contract for the hackathon. Its provenance must remain visible and separate from claims about actual company performance. A report called “in transit” is not assumed to contain only pipeline data, and monthly zero/blank balances are not assumed to be exact stockout events.
