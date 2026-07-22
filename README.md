# mcp-gen-comparison

A research study benchmarking different ways to **automatically generate an MCP server** that wraps the Finnish **LVI-INFO Read-API** (a building-products catalog for HVAC, plumbing, and electrical supplies). Each generation strategy is called an **arm**. All arms produce a [FastMCP](https://github.com/jlowin/fastmcp) (Python, stdio transport) MCP server. A single shared **harness** grades every arm identically against one fixed rubric.

**Research question:** *Is fully automated MCP generation feasible, and which approach should be the backbone?* The key discriminator is whether an agentic arm can synthesize capability that the underlying API doesn't natively expose — specifically, ETIM technical-data filtering — versus merely mirroring the endpoints (which a deterministic converter already does perfectly).

---

## Table of contents

1. [Quick start](#quick-start)
2. [Background: what is LVI-INFO?](#background-what-is-lvi-info)
3. [The API in depth](#the-api-in-depth)
4. [The ETIM discriminator — why it matters](#the-etim-discriminator--why-it-matters)
5. [Repository layout](#repository-layout)
6. [Arms (generation strategies)](#arms-generation-strategies)
7. [The harness](#the-harness)
8. [Results so far](#results-so-far)
9. [Adding a new arm](#adding-a-new-arm)
10. [Golden rules](#golden-rules)

---

## Quick start

### Prerequisites

- Python 3.11+
- A valid LVI-INFO QA API key

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` currently pins `fastmcp>=3.4.4` and `python-dotenv>=1.2.2`. All arms use `httpx` for HTTP, which FastMCP pulls in transitively.

### Configure

```bash
cp .env.example .env
# Edit .env and fill in your real values:
#   LVI_API_KEY=<your key>
#   LVI_API_BASE=https://search-api.lvi-info.fi.qa.ambientia.fi
```

The key is passed as a query parameter (not a header) on every request. Never commit `.env`.

### Verify the harness is sane (offline, no API key needed)

```bash
python harness/harness.py --selfcheck --fixture fixtures/onninen_8293_page0.json
```

All checks should print `[PASS]`. Run this before grading any arm.

### Grade an arm (live, hits the QA API)

```bash
python harness/harness.py --arm handauthored --server arms/handauthored/server.py
python harness/harness.py --arm from_openapi  --server arms/from_openapi/server.py
python harness/harness.py --arm gsd           --server arms/gsd/server.py
python harness/harness.py --arm plain         --server arms/plain/server.py
```

Results are appended to `results/results.csv` automatically.

---

## Background: what is LVI-INFO?

LVI-INFO is a Finnish industry catalog covering HVAC (lämpö, vesi, ilma — LVI), plumbing, and electrical supply products. Manufacturers and distributors publish structured product data to it. The catalog holds roughly 260,000 products across ~400 suppliers. The Read-API is the programmatic interface to that data; the goal of this project is to expose it over MCP so an LLM agent can query it without writing raw HTTP calls.

---

## The API in depth

**Environment:** QA only — `https://search-api.lvi-info.fi.qa.ambientia.fi` (set as `LVI_API_BASE`). QA data drifts over time; always note the test date when recording results.

**Auth:** `apiKey` is a query parameter appended to every request (e.g. `?apiKey=...`). There is no Authorization header.

### Endpoints

| Endpoint | What it does |
|---|---|
| `GET /api/v1/products/full-data` | Paginated product list with filters |
| `GET /api/v1/products/full-data/{productLinkNumber}` | Single product by ProductLinkNumber |
| `GET /api/v1/products/full-data/gtin/{gtin}` | Single product by GTIN-13 |
| `GET /api/v1/products/meta/suppliers` | Suppliers visible to this API key |
| `GET /api/v1/products/changed` | LVI numbers changed in a date range |

### full-data query parameters

`page` (default 0), `pageSize` (1–1000, default 50), `supplierNumber`, `lviNumber`, `businessID`, `productType` (`M` brand-specific or `Y` shared/generic), `hasAttachments`, `isActive`, `publishedDateAfter`, `publishedDateBefore`, `lastModifiedAfter`, `lastModifiedBefore`.

### Response shape

```json
{
  "page": 0,
  "totalResults": 10000,
  "totalPages": 200,
  "products": [
    {
      "TT020": "0101202",
      "TT024": "01012028293",
      "TT110": "ONNINEN",
      "TT200": "Paineputki sg-rautaa DN 150",
      "etimFeatureValues": [
        {
          "etimClassId": "EC011303",
          "etimFeatureId": "EF006272",
          "etimFeatureType": "NUMERIC",
          "value1": "150",
          "unitOfMeasureAbbreviation": "mm"
        }
      ]
    }
  ]
}
```

Products use `TTxxx`-coded field names. Populated fields vary by product (15–40 fields); empty fields are omitted. The full field dictionary is in `arms/plain/server.py` (the `DETAIL_GROUPS` mapping).

### Known edge cases

| Situation | What the API does |
|---|---|
| Bad API key | HTTP 403 |
| Page × pageSize > 15000 | HTTP 500 "all shards failed" — **not** a 404 or useful message |
| Query matches nothing | HTTP 200 with `products: []` and `totalResults: 0` — this is a normal empty result, not an error |
| `totalResults` ≥ 10000 | Capped at 10000 by the backend; real count may be higher |
| Large suppliers | Must be paged by `supplierNumber` to avoid the 15000-index ceiling |

---

## The ETIM discriminator — why it matters

ETIM (European Technical Information Model) is a technical-data standard for building-products. Each product can have an `etimFeatureValues` array describing measurable technical attributes — e.g. class `EC011303` (steel pipe), feature `EF006272` (nominal diameter, numeric, in mm).

**The API has no query parameter for ETIM.** You cannot say "give me products with nominal diameter 150 mm." The only way to filter by ETIM is:

1. Fetch candidate pages (optionally narrowed by supplier/LVI number).
2. Filter `etimFeatureValues` in memory.

This is the test that separates arms that *mirror the API* from arms that *synthesize new capability*. A raw OpenAPI conversion cannot produce an ETIM filter tool because there is no corresponding API parameter. A hand-authored or sufficiently capable agentic arm can.

**Frozen discriminator values (from supplier 8293, Onninen):**
- ETIM class: `EC011303`
- ETIM feature: `EF006272` (nominal diameter)
- Products that carry it: `0101202` (Paineputki sg-rautaa DN 150) and `0101203`

---

## Repository layout

```
mcp-gen-comparison/
├── fixtures/
│   └── onninen_8293_page0.json   # Real captured page-0 from supplier 8293 (50 products,
│                                 # envelope shows true totals ~10000). Committed. Do not
│                                 # modify without re-capturing from the live API.
├── harness/
│   ├── harness.py                # The shared grader (neutral referee — never tune to one arm)
│   └── expected.py               # Frozen known-good values: LVI numbers, ETIM IDs, aliases
├── arms/
│   ├── from_openapi/
│   │   ├── server.py             # FastMCP OpenAPI auto-conversion (deterministic baseline)
│   │   ├── binding.json          # Maps each check → tool name + args for this arm
│   │   └── openapi.json          # Pinned OpenAPI spec used for generation
│   ├── handauthored/
│   │   ├── server.py             # Human-designed gold standard (ceiling)
│   │   └── binding.json
│   ├── gsd/
│   │   ├── server.py             # GSD spec-driven agentic generation (claude-sonnet-5)
│   │   ├── binding.json
│   │   └── arm_info.json         # Generation metadata: model, tokens, time, score, notes
│   └── plain/
│       ├── server.py             # Plain prompting, no special technique
│       └── binding.json
├── results/
│   └── results.csv               # One row per arm run (appended by harness)
├── requirements.txt
├── .env.example                  # Safe template — copy to .env and fill in
├── .gitignore                    # .env, __pycache__, .venv
└── CLAUDE.md                     # Instructions for AI assistants working in this repo
```

---

## Arms (generation strategies)

### `from_openapi` — deterministic baseline

**How generated:** `fastmcp` can auto-generate an MCP server from an OpenAPI spec with one function call. `arms/from_openapi/openapi.json` is the pinned spec. The server exposes exactly what the spec describes — 5 tools: `publicSearch`, `singleFullData`, `singleByGtin`, `getSuppliers`, `getChanged`.

**What it can and cannot do:** It correctly handles all API-native filtering. It cannot do ETIM filtering (no API param → no tool). It does not project/trim fields. It does not clamp `pageSize`. It passes raw API error envelopes back to the caller.

**Score: 4/10** (reproducible, instant, zero human effort)

---

### `handauthored` — gold standard / ceiling

**How generated:** Written by hand to be the best possible server from a human expert who fully understood both the API and the MCP use-case. This is the ceiling that automated arms are compared against.

**Design decisions:**
- Tools named around *tasks* (lookup, search, ETIM filter), not 1:1 with API endpoints.
- Products are projected to a handful of useful fields (`TT020`, `TT024`, `TT110`, `TT200`, `TT201`, `TT052`, `TT060`, `TT025`) plus `etimFeatureValues`. The full 15–40-field raw response is too noisy for an LLM.
- `filter_by_etim_class` synthesizes ETIM filtering: paginates over candidates (narrowed by `supplier_number`/`lvi_number`/`business_id`), filters `etimFeatureValues` in memory, reports coverage honestly if the candidate set was too large to fully scan.
- `pageSize` is always clamped to [1, 1000] before sending.
- Every tool catches API failures and returns a structured, explained result instead of leaking the raw `{timestamp, status, error, path}` envelope.

**Tools (7):** `lookup_by_lvi_number`, `get_product_by_link`, `get_product_by_gtin`, `search_products`, `filter_by_etim_class`, `list_suppliers`, `get_changed_products`.

**Score: 9/10** (NA on `pagination_ceiling` because supplier 8293 only has ~10,000 products, below the 15,000 ceiling where the bug triggers)

---

### `gsd` — spec-driven agentic (claude-sonnet-5 / "claude-sonnet-5")

**How generated:** The GSD (Get Stuff Done) workflow was used: discuss → plan → execute. Model: `claude-sonnet-5`. Generation took ~16 minutes, ~10M tokens (mostly cache reads), cost ~$4.60.

**What it got right:** Recognized the `pageSize` limit and the 15,000-index ceiling. Built `search_products_by_keyword` (client-side text search, not in the API). Noticed ETIM data and wrote a `_reshape_etim_features` helper. Handled auth errors cleanly.

**What it under-implemented:** Built no ETIM filter tool despite recognizing the ETIM data. The `_reshape_product` function keeps all 38 fields (projection gesture without actual reduction). The `empty_vs_error` case is documented in docstrings but the tool still passes a raw empty envelope. Pattern: *understood the hard problems, under-implemented all three.*

**Tools (6):** `list_products`, `get_product`, `get_product_by_gtin`, `list_suppliers`, `get_changed_products`, `search_products_by_keyword`.

**Score: 5/10**

---

### `plain` — plain prompting, no special technique

**How generated:** A single prompt to Claude with the API description and a request to build an MCP server. No spec-driven workflow, no iteration.

**What it got right:** `pageSize` clamping, auth error handling, keyword search. Good field translation (TT codes → readable English names, grouped by category). Notably richer than GSD in some respects despite simpler generation.

**What it missed:** ETIM filter tool (no in-memory filtering synthesized). Early runs failed `lookup_by_lvi` (field naming mismatch vs. harness alias list) — fixed by harness update to recognize the arm's field names.

**Tools (7):** `search_products`, `get_product`, `get_product_by_gtin`, `get_products_by_lvi_number`, `search_products_by_keyword`, `list_suppliers`, `list_changed_products`.

**Score: 6/10** (latest run)

---

### Planned arms

| Arm | Strategy |
|---|---|
| `mcpybarra` | Agentic gen → automated QA → refine loop |
| `combined` | Deterministic `from_openapi` backbone + agent layer for the parts (ETIM) that have no endpoint |

---

## The harness

### Design principles

The harness is the **neutral referee**. It must never be tuned to make a specific arm pass. It is written once, against frozen expected values, and run identically against every arm. Changing it to make an arm pass invalidates the comparison.

The chain under test: `harness.py → arm's server.py (stdio) → LVI-INFO QA API`.

### How it works

1. Reads `arms/<arm>/binding.json` — a map from each check name → `{tool, args}`. A missing or null binding means the arm has no tool for that check → that check **FAILS**.
2. Launches the arm's `server.py` as a subprocess (FastMCP stdio transport), passing the full environment including `LVI_API_KEY`.
3. Lists tools (`list_tools()`), then calls each bound tool.
4. Validates each result with a check-specific validator. Validators don't assume a fixed response shape — they recursively search the returned structure for the expected values, so arms with different field naming conventions all work.
5. `auth_fail` is special: it relaunches the server in a separate subprocess with a deliberately invalid key, then checks the error is surfaced cleanly.
6. Appends one CSV row to `results/results.csv`.

### Field-name agnosticism

Arms will inevitably name things differently. The harness copes by checking *values* under any of a set of alias keys defined in `harness/expected.py`:

```python
LVI_NUMBER_ALIASES        = ("TT020", "lvi_number", "lviNumber")
PRODUCT_LINK_NUMBER_ALIASES = ("TT024", "product_link_number", "productLinkNumber")
SUPPLIER_ENDSWITH_ALIASES = ("TT024", "product_link_number", "productLinkNumber")  # value ends in supplier number
SUPPLIER_EQUALS_ALIASES   = ("supplier_number", "supplierNumber")
SUPPLIER_TEXT_ALIASES     = ("TT100", "supplier")
```

If a new arm uses a field name not in these lists, add the alias to `expected.py` (which is acceptable — alias lists are intentionally broad, not arm-specific).

### The 10 checks

| Check | What it tests |
|---|---|
| `lookup_by_lvi` | Look up product `0101202` → response contains a product with LVI number `0101202` |
| `lookup_by_link` | Look up ProductLinkNumber `01012028293` → same product found |
| `supplier_filter` | Filter by `supplierNumber=8293` → ≥1 product returned, all belong to that supplier |
| `etim_filter` | **Discriminator** — filter by ETIM class `EC011303` / feature `EF006272` → includes `0101202` and `0101203` |
| `projection` | Look up `0101202` → returned product object has ≤12 fields (trimmed, not raw dump) |
| `pagesize_clamp` | Request `pageSize=5000` (API max is 1000) → clamped or rejected cleanly, no crash |
| `pagination_ceiling` | Request a page past result index 15000 → handled gracefully, no raw 500 leak *(NA for arms that only bind supplier 8293, which has only ~10,000 products)* |
| `bad_input` | Look up non-existent LVI number `9999999` → clean "not found" signal, not a crash |
| `empty_vs_error` | Over-constrained query that returns zero results → clear "no matches" message, not a raw empty envelope |
| `auth_fail` | Server launched with invalid key → clean auth error surfaced (not a raw 403 envelope) |

### Leak detection

The harness flags any result that contains:
- Python traceback text (`Traceback (most recent call last)`)
- Raw httpx or requests exception strings
- The LVI-INFO API's error envelope (detected by the presence of ≥3 of: `timestamp`, `status`, `error`, `path` — the JSON keys the API sends on failures)

### Binding.json format

```json
{
  "check_name": {
    "tool": "tool_name_in_this_arm",
    "args": { "arg1": "value1" }
  },
  "etim_filter": null
}
```

Set a check to `null` (or omit it) to declare the arm has no tool for it — the check will FAIL. For `pagination_ceiling`, `null` produces **NA** instead of FAIL, because it's not a fair test unless the bound supplier has >15,000 products.

### Selfcheck mode

```bash
python harness/harness.py --selfcheck --fixture fixtures/onninen_8293_page0.json
```

Runs entirely offline (no server, no API key). Validates that:
- The ETIM helper finds products `0101202` and `0101203` in the fixture.
- The product finder locates the known product.
- `find_all_products` counts all 50 fixture products.
- The projection field counter distinguishes raw (>12 fields) from trimmed (≤12 fields).
- Empty envelope detection returns no products.
- The bad LVI number is absent from the fixture.

**Always run selfcheck before grading a new arm**, especially after any harness change.

---

## Results so far

As of 2026-07-13 (latest run per arm):

| Arm | Score | Notes |
|---|---|---|
| `handauthored` | **9/10** | Gold standard ceiling. Fails only `bad_input` in one run (flaky). |
| `plain` | **6/10** | No ETIM tool. Good field translation. `bad_input` flaky. |
| `gsd` | **5/10** | Understood the hard problems; under-implemented ETIM, projection, empty-vs-error. |
| `from_openapi` | **4/10** | Deterministic baseline. No ETIM, no projection, no error handling. |

All arms are NA on `pagination_ceiling` (supplier 8293 only has ~10,000 products).

Full run history is in `results/results.csv`.

### Question B — generation cost (logged by hand)

The harness measures server quality (Question A). Generation cost (Question B) is recorded manually per arm:

| Metric | What to log |
|---|---|
| **Model** | The specific model used (it's part of what's being tested) |
| **Tokens** | Input, output, cache read/write separately |
| **Wall-clock time** | From first prompt to runnable server |
| **Human interventions** | Code edits, corrections, re-runs needed |
| **Iterations** | How many generation cycles |
| **Reproducibility** | Run the arm generation twice; diff the outputs |

The GSD arm's generation metadata is in `arms/gsd/arm_info.json`.

---

## Adding a new arm

1. Create `arms/<name>/server.py` — a FastMCP server that runs over stdio (`mcp.run()` in `__main__`). Read `LVI_API_KEY` and `LVI_API_BASE` from the environment (never hardcode).

2. Create `arms/<name>/binding.json` mapping each of the 10 check names to the tool and args that arm exposes. Set unused checks to `null`.

3. Run selfcheck to confirm the harness is still sound:
   ```bash
   python harness/harness.py --selfcheck --fixture fixtures/onninen_8293_page0.json
   ```

4. Grade the arm:
   ```bash
   python harness/harness.py --arm <name> --server arms/<name>/server.py
   ```

5. Log generation cost metadata (model, tokens, time, interventions) either in `arms/<name>/arm_info.json` or in the commit message.

---

## Golden rules

1. **The harness is the neutral referee. Never tune it to any one arm's output.** It is written once, against fixed values, and run identically against every arm. Changing it to make an arm pass invalidates the comparison.
2. **The harness grades a *generated* MCP server, not the API directly.** Chain is: `harness.py → arm's server.py (stdio) → LVI-INFO API`.
3. **Never commit `.env`** — it holds the live API key. `.env.example` is the safe template.
4. **Never hardcode the API key** in any server, arm, or test file. Read it from `LVI_API_KEY` in the environment.
5. **Keep the repo inside the WSL filesystem** (`~/mcp-gen-comparison`), not under `/mnt/c/...` — git and Python are significantly slower on the Windows mount.
6. **QA data drifts.** Always record the test date when capturing new fixture data or noting totalResults counts.
