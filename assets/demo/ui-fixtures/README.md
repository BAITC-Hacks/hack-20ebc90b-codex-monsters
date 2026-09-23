# Synthetic UI response samples

`bundle.json` contains only invented data for the explicit **«Демо: имитация API»** mode. It is a set of prepared API responses, not a forecast, ingestion pipeline, planning engine, or evidence of a live integration. `fixture:` artifact references are illustrative identifiers; no real Parquet source or commercial data is included.

The fixed context is `demo-snapshot`, `demo-run`, seed `42`, and `2026-09-01T00:00:00+00:00`. There are two suppliers and one warehouse. The initial proposals are:

| Proposal | Line | Selected purchase quantity | Base quantity |
|---|---|---|---|
| `demo-proposal-tools` | `line-tools` | 108 pieces | 108 pieces |
| `demo-proposal-cable` | `line-cable` | 3 coils | 300 metres |

The tool line illustrates the documented golden ledger: `100 + 20 + 10 + 30 − 40 − 20 + 8 + 0 = 108`. The cable line illustrates base versus purchase UOM and an explicitly late pipeline. `INVALID-MOQ` is excluded with a reason. A suspected project event remains visible as uncertain demand; it is not presented as a confirmed project or used as a customer identifier.

Mock edits choose complete prepared line variants. Supported quantities are `0/108/120/132` for tools and `0/2/3/4/5/6` for cable. Other quantities return `422 MOCK_UNSUPPORTED_QUANTITY`; no general MOQ, rounding, safety-stock, or planning formula runs in the UI. Each edit creates a new draft version and a separate manual adjustment in the explanation. Approval and CSV export are simulated inside `MockClient`, which loses its state when the session is reset. HTTP mode returns server CSV bytes unchanged.

There are two isolated scenario samples with no budget cap:

- Target 99%, delay 0: tools 120 pieces with safety stock 48; cable remains 3 coils with safety stock 70. The prepared total is KZT 15,000. Higher safety stock need not change an already rounded quantity.
- Target 95%, delay 7 days: tools 144 pieces with safety stock 40; cable 4 coils with safety stock 55. The prepared total is KZT 18,400.

Other scenario combinations return `422 MOCK_UNSUPPORTED_SCENARIO`. These samples cannot demonstrate a live recalculation or measured financial improvement. Scenario results do not modify the base proposals or approvals.

`MockClient(quality="degraded")` removes costs and disables budget comparison. `MockClient(quality="blocked")` adds explicit missing-terms issues and blocks planning, approval, and export. Both are synthetic examples of missing data, not imported real data. Decimal quantities and monetary values are strings; missing monetary values remain `null`.
