# edpb-core

The shared EDPB pipeline **contract**, consumed by all three repos
(`EDP_Billing`, `EDPBilling_FIle_Upload`, `mofsl_file_download_rpa_bot`):

| Module | Owns |
|---|---|
| `edpb_core.segments` | the 9 segment codes, post-trade order, bot-downloadable subset |
| `edpb_core.dates` | folder (`DD-MM-YYYY`) ↔ ISO ↔ CBOS `MARGINDATE` conversions |
| `edpb_core.correlation` | `X-Request-ID` convention + per-run id mint |
| `edpb_core.manifest` | THE `manifest.schema.json` + load/validate helpers |
| `edpb_core.batch_api` | uploader batch statuses + endpoint paths |
| `edpb_core.cbos` | CBOS **v6** endpoint paths + payload builders (Shape A/B, CHECKINSTITRADE) |
| `edpb_core.mock_cbos` | THE mock CBOS server (v6) — `uvicorn edpb_core.mock_cbos.app:app --port 8009` (needs the `[mock]` extra) |

Design rule: this package owns **wire shapes and vocabulary**, never
transport. Each service keeps its own thin HTTP client (sync/async as it
needs) built on these constants — shapes drift when copied; clients don't
need sharing to stay correct.

Consumed as an editable path dependency (repos are sibling checkouts):

```toml
# sibling repo pyproject.toml
[tool.uv.sources]
edpb-core = { path = "../EDP_Billing/packages/edpb-core", editable = true }
```

Prose contracts these types encode:
`EDPBilling_FIle_Upload/docs/BATCH_HANDOFF_CONTRACT.md` and
`docs/CBOS_HANDOFF_CONTRACT.md`.
