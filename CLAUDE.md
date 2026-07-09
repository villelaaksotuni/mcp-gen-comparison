# CLAUDE.md — mcp-gen-comparison

## What this project is

A research study benchmarking different ways of **automatically generating an MCP
server** that wraps the Finnish **LVI-INFO Read-API** (a building-products catalog).
Each generation approach is an **"arm."** All arms produce a FastMCP (Python) MCP
server. A single shared **harness** grades every arm's server against one fixed rubric,
so the arms are compared identically.

The research question: *is fully automated MCP generation feasible, and which approach
should be the backbone?* The interesting finding is expected to be whether an agentic
arm can synthesize capability the API does not natively expose (see ETIM filtering
below), versus merely mirroring the endpoints (which a deterministic converter already
does).

## Golden rules for working in this repo

1. **The harness is the neutral referee. Never tune it to any one arm's output.**
   It is written once, against the fixed values below, and run identically against
   every arm. Changing it to make an arm pass invalidates the comparison.
2. **The harness grades a *generated MCP server*, not the API directly.** Chain is:
   `harness.py → an arm's MCP server (stdio) → LVI-INFO API`.
3. **Never commit `.env`** (it holds the live API key). It is gitignored. `.env.example`
   is the safe template.
4. **Never hardcode the API key** in any server, arm, or test file. Read it from
   `LVI_API_KEY` in the environment / `.env`.
5. **Keep the repo inside the WSL filesystem** (`~/mcp-gen-comparison`), never under
   `/mnt/c/...` — git and node are far slower on the Windows mount.
6. Prefer **simple, readable** code over frameworks. Adequate and debuggable beats clever.

## The API (LVI-INFO Read-API)

- **Environment: QA only** — `https://search-api.lvi-info.fi.qa.ambientia.fi`
  (set as `LVI_API_BASE` in `.env`). QA data drifts over time; record the test date.
- **Auth:** `apiKey` is a **query parameter** on every request (not a header).
- **Endpoints:**
  - `GET /api/v1/products/full-data` — list; query params below
  - `GET /api/v1/products/full-data/{productLinkNumber}` — single product
  - `GET /api/v1/products/full-data/gtin/{gtin}` — single product by GTIN-13
  - `GET /api/v1/products/meta/suppliers` — suppliers this key can see
  - `GET /api/v1/products/changed` — changed LVI-numbers in a date range
- **full-data query params:** `page` (default 0), `pageSize` (**1–1000**, default 50),
  `supplierNumber`, `lviNumber`, `businessID`, `productType` (`M` or `Y`),
  `hasAttachments`, `isActive`, and date filters.
- **Response shape:**
  `{ page, totalResults, totalPages, products: [ { TT-fields..., etimFeatureValues: [...] } ] }`
- **Each product** has 15–40 populated `TTxxx` fields. Empty fields are omitted.
- **ETIM tech data is NESTED inside each product** as
  `etimFeatureValues: [ { etimClassId, etimFeatureId, etimFeatureType, value1, value2,
  unitOfMeasureAbbreviation, etimUnitOfMeasureId } ]`.
  **There is NO query parameter to filter by ETIM class or feature.** ETIM filtering can
  only be done **in memory** after fetching. This is the key discriminator between arms.

### Error / edge behavior (observed live)
- Consistent error envelope: `{ timestamp, status, error, path }`.
- **Bad API key → HTTP 403.**
- **Result index past 15000 → HTTP 500 "all shards failed"** (even with pagination).
  To fetch large sets, split by `supplierNumber`.
- **Over-constrained query → HTTP 200 with `products: []` and `totalResults: 0`** — this
  is NOT an error. A good server must distinguish "no matches" from "something failed."

## Frozen known-good test values (source of truth for the harness)

Captured live from supplier 8293 (Onninen) and saved in the fixture. Do not change these
without re-capturing from the live API.

- **Lookup by LVI-number:** `0101202` → "Paineputki sg-rautaa DN 150", brand `ONNINEN`,
  ETIM class `EC011303`. Has populated ETIM features.
- **Single by ProductLinkNumber:** `01012028293` (same product as 0101202).
- **Supplier filter:** `supplierNumber = 8293` (Onninen; totalResults ≈ 10000).
- **ETIM discriminator:** class `EC011303`, feature `EF006272` (nominal diameter,
  NUMERIC, unit `mm`). Products `0101202` and `0101203` carry it.
- **Bad LVI-number (not found):** `9999999`.
- **Fixture:** `fixtures/onninen_8293_page0.json` — one real page (50 products, envelope
  keeps the true totals 10000/200). The two ETIM-bearing products are 0101202 and 0101203.

## Repo layout

```
fixtures/onninen_8293_page0.json   real captured page-0 response (committed)
harness/harness.py                 the shared tester (grades any arm's server)
harness/expected.py                frozen known-good values (above)
arms/<arm>/server.py               that arm's MCP server (stdio entry point)
arms/<arm>/binding.json            maps each harness check -> this arm's tool name + args
results/results.csv                one row per arm run (Question A: server quality)
.env / .env.example                LVI_API_KEY, LVI_API_BASE
```

Arms planned: `from_openapi` (deterministic FastMCP baseline), `handauthored` (gold
standard / ceiling), `gsd` (spec-driven agent), `mcpybarra` (agentic gen→QA→refine),
`combined` (deterministic backbone + agent for the parts that have no endpoint).

## The harness (how grading works)

- Connects to a server over **stdio** (launches `server.py` as a subprocess, passes the
  environment including `LVI_API_KEY`). Uses the `fastmcp` Client — check the installed
  fastmcp version and use its current stdio Client API.
- Reads `arms/<arm>/binding.json` to map each check to the arm's actual tool + args.
  A missing mapping means "this arm has no tool for this check" → that check **FAILS**.
- Records tool count + names from `list_tools()` (tool-design-quality signal).
- Two modes:
  - `--selfcheck --fixture fixtures/onninen_8293_page0.json` — offline; validates the
    harness's OWN logic against the fixture (ETIM helper finds 0101202/0101203, projection
    field-counter works, empty-detection works). Run this BEFORE grading any server.
  - `--arm <name> --server arms/<name>/server.py` — live; launches the server, runs all
    checks via the binding.
- Appends one row to `results/results.csv`:
  `arm, timestamp, n_tools, tool_names, <PASS/FAIL/NA per check>, total_passed`.

### Checks (fixed rubric)
1. `lookup_by_lvi` — lookup 0101202 → product with TT020 == "0101202".
2. `lookup_by_link` — single by 01012028293 → TT020 == "0101202".
3. `supplier_filter` — supplierNumber 8293 → ≥1 product, all belong to 8293 (TT024 ends "8293").
4. `etim_filter` — **discriminator** — products in ETIM class EC011303 (feature EF006272 if
   supported) → includes 0101202/0101203. **No such tool = FAIL.**
5. `projection` — any one product returns a TRIMMED object (≤ ~12 fields), not the raw 20+.
6. `pagesize_clamp` — pageSize 5000 (API max 1000) → clamped/rejected cleanly, no crash.
7. `pagination_ceiling` — page past result-index 15000 → handled, not a raw 500 leak.
   (Supplier 8293 has only 10000; mark **N/A** unless a >15000 supplier is bound.)
8. `bad_input` — lookup 9999999 → clean "not found", not a crash.
9. `empty_vs_error` — over-constrained query returning [] → clear "no matches" signal,
   not a raw empty envelope.
10. `auth_fail` — server launched with a bad key → clean auth error, not a raw 403 leak.

## What the harness does NOT measure

Question B (generation cost) is logged **by hand when generating each arm**, not by the
harness: **tokens used, wall-clock time, human interventions (code edits) to reach passing,
iterations, and reproducibility** (run an arm twice, diff the output). Also pin and record
the **model** used for any agentic arm — it is part of what's being tested.
