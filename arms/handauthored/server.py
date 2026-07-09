#!/usr/bin/env python3
"""handauthored arm — gold standard / ceiling.

A human-designed FastMCP server wrapping the LVI-INFO Read-API, built to be
genuinely useful to an LLM agent rather than a literal mirror of the spec:

- Tools are named/shaped around tasks (lookup, search, ETIM filter), not
  1:1 with endpoints.
- Product objects are trimmed to a handful of the fields an agent actually
  needs. The original TT-code keys (TT020 lviNumber, TT024 productLinkNumber,
  etc.) are kept as-is rather than renamed — they're the catalog's real
  identifiers and every downstream LVI-INFO consumer already knows them.
- ETIM class/feature filtering has no query param in the API, so
  filter_by_etim_class synthesizes it: fetch candidate pages, filter
  in-memory. This is the capability raw OpenAPI conversion cannot produce.
- Every tool catches API failures (auth, 4xx/5xx, network) and returns a
  clean, explained result instead of raising a raw exception or leaking the
  API's {timestamp,status,error,path} envelope.
- pageSize is always clamped to the API's [1, 1000] range before the request
  is sent, with a note in the response if clamping happened.
"""

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

API_BASE = os.environ["LVI_API_BASE"]
API_KEY = os.environ["LVI_API_KEY"]

FULL_DATA_PATH = "/api/v1/products/full-data"
SUPPLIERS_PATH = "/api/v1/products/meta/suppliers"
CHANGED_PATH = "/api/v1/products/changed"

API_MAX_PAGE_SIZE = 1000
# The API 500s ("all shards failed") once a query's result index passes this,
# even with pagination. Paginating tools stay under it rather than crash into
# it; see CLAUDE.md for the observed behavior this is based on.
API_RESULT_INDEX_CEILING = 15000

# The handful of TT-fields worth showing an agent by default: lviNumber,
# productLinkNumber, brand, product name, product model/variant, GTIN,
# ETIM class ID, product group. Everything else (customs codes, raw
# quantity/weight breakdowns, attachment URLs, ...) is dropped unless asked
# for via the underlying fields directly.
PROJECTED_FIELDS = ["TT020", "TT024", "TT110", "TT200", "TT201", "TT052", "TT060", "TT025"]

client = httpx.AsyncClient(base_url=API_BASE, timeout=30.0)

mcp = FastMCP(name="LVI-INFO (handauthored gold standard)")


class LviApiError(Exception):
    """A clean, human-readable description of an API-level failure. Tools
    catch this and fold it into their normal return value instead of letting
    it surface as a raw exception."""


async def _get_json(path: str, params: dict[str, Any]) -> Any | None:
    """GET path with the API key attached; returns parsed JSON, None on 404,
    or raises LviApiError with an explained (never raw) message."""
    query = {"apiKey": API_KEY, **{k: v for k, v in params.items() if v is not None}}
    try:
        response = await client.get(path, params=query)
    except httpx.RequestError as e:
        raise LviApiError(f"Could not reach the LVI-INFO API: {e}") from e

    if response.status_code == 404:
        return None
    if response.status_code == 403:
        raise LviApiError(
            "Authentication failed: the LVI-INFO API rejected the configured "
            "API key (HTTP 403). Check LVI_API_KEY in the environment."
        )
    if response.status_code >= 500:
        raise LviApiError(
            "The LVI-INFO API failed to process this request (server-side "
            f"error, HTTP {response.status_code}). This usually means the "
            "query's result index went past the API's internal pagination "
            "limit -- narrow the query (e.g. by supplierNumber) or request "
            "an earlier page."
        )
    if response.status_code >= 400:
        raise LviApiError(f"The LVI-INFO API rejected this request (HTTP {response.status_code}).")
    return response.json()


def _project(product: dict) -> dict:
    trimmed = {field: product[field] for field in PROJECTED_FIELDS if field in product}
    if product.get("etimFeatureValues"):
        trimmed["etimFeatureValues"] = product["etimFeatureValues"]
    return trimmed


async def _fetch_all_pages(params: dict[str, Any], max_pages: int) -> tuple[list[dict], int | None, int]:
    """Page through full-data with page_size=1000, stopping at whichever of
    (max_pages, all results fetched, the API's known result-index ceiling)
    comes first. Returns (products, total_results, pages_fetched)."""
    hard_cap = API_RESULT_INDEX_CEILING // API_MAX_PAGE_SIZE
    max_pages = max(1, min(max_pages, hard_cap))
    products: list[dict] = []
    total_results = None
    pages_fetched = 0
    for page in range(max_pages):
        data = await _get_json(FULL_DATA_PATH, {**params, "page": page, "pageSize": API_MAX_PAGE_SIZE})
        pages_fetched += 1
        total_results = data.get("totalResults", total_results)
        page_products = data.get("products", [])
        products.extend(page_products)
        if not page_products or (total_results is not None and len(products) >= total_results):
            break
    return products, total_results, pages_fetched


@mcp.tool
async def lookup_by_lvi_number(lvi_number: str) -> dict:
    """Look up a single product by its LVI number (the LVI-INFO catalog
    identifier, e.g. "0101202"). If no such product exists, returns
    found=False with a clear reason -- that is a normal miss, not an error."""
    try:
        data = await _get_json(FULL_DATA_PATH, {"lviNumber": lvi_number, "pageSize": 1})
    except LviApiError as e:
        return {"found": False, "lviNumber": lvi_number, "reason": str(e)}
    products = (data or {}).get("products", [])
    if not products:
        return {"found": False, "lviNumber": lvi_number, "reason": f"No product found with LVI number {lvi_number}."}
    return {"found": True, "product": _project(products[0])}


@mcp.tool
async def get_product_by_link(product_link_number: str) -> dict:
    """Look up a single product by its ProductLinkNumber (LVI number +
    supplier number concatenated, e.g. "01012028293")."""
    try:
        data = await _get_json(f"{FULL_DATA_PATH}/{product_link_number}", {})
    except LviApiError as e:
        return {"found": False, "productLinkNumber": product_link_number, "reason": str(e)}
    if data is None:
        return {
            "found": False,
            "productLinkNumber": product_link_number,
            "reason": f"No product found with product link number {product_link_number}.",
        }
    return {"found": True, "product": _project(data)}


@mcp.tool
async def get_product_by_gtin(gtin: str) -> dict:
    """Look up a single product by its GTIN-13 barcode."""
    try:
        data = await _get_json(f"{FULL_DATA_PATH}/gtin/{gtin}", {})
    except LviApiError as e:
        return {"found": False, "gtin": gtin, "reason": str(e)}
    if data is None:
        return {"found": False, "gtin": gtin, "reason": f"No product found with GTIN {gtin}."}
    return {"found": True, "product": _project(data)}


@mcp.tool
async def search_products(
    supplier_number: str | None = None,
    lvi_number: str | None = None,
    business_id: str | None = None,
    product_type: str | None = None,
    has_attachments: bool | None = None,
    is_active: bool | None = None,
    published_date_after: str | None = None,
    published_date_before: str | None = None,
    last_modified_after: str | None = None,
    last_modified_before: str | None = None,
    page: int = 0,
    page_size: int = 50,
) -> dict:
    """Search products with any combination of filters. page_size is
    clamped to the API's [1, 1000] range. A filter combination that matches
    nothing returns an empty products list with an explanatory reason, not
    an error -- that's a normal "no matches", not a failure."""
    clamped_page_size = max(1, min(page_size, API_MAX_PAGE_SIZE))
    params = {
        "supplierNumber": supplier_number,
        "lviNumber": lvi_number,
        "businessID": business_id,
        "productType": product_type,
        "hasAttachments": has_attachments,
        "isActive": is_active,
        "publishedDateAfter": published_date_after,
        "publishedDateBefore": published_date_before,
        "lastModifiedAfter": last_modified_after,
        "lastModifiedBefore": last_modified_before,
        "page": page,
        "pageSize": clamped_page_size,
    }
    try:
        data = await _get_json(FULL_DATA_PATH, params)
    except LviApiError as e:
        return {"page": page, "pageSize": clamped_page_size, "totalResults": 0, "products": [], "reason": str(e)}

    products = data.get("products", [])
    result = {
        "page": data.get("page", page),
        "pageSize": clamped_page_size,
        "totalResults": data.get("totalResults", 0),
        "totalPages": data.get("totalPages", 0),
        "products": [_project(p) for p in products],
    }
    if page_size > API_MAX_PAGE_SIZE:
        result["note"] = (
            f"Requested pageSize {page_size} exceeds the API maximum of "
            f"{API_MAX_PAGE_SIZE}; clamped to {API_MAX_PAGE_SIZE}."
        )
    if not products:
        result["reason"] = "No matches found for the given filters."
    return result


@mcp.tool
async def filter_by_etim_class(
    etim_class_id: str,
    etim_feature_id: str | None = None,
    supplier_number: str | None = None,
    lvi_number: str | None = None,
    business_id: str | None = None,
    max_pages: int = 15,
) -> dict:
    """Find products carrying a given ETIM technical-data class (and,
    optionally, a specific feature within it) -- e.g. class EC011303,
    feature EF006272 (nominal diameter). The API has no query parameter for
    this: it fetches candidate pages (narrowed by supplier_number/
    business_id/lvi_number if given) and filters in-memory. Without a
    narrowing filter, coverage is capped at max_pages * 1000 products and the
    result says so rather than silently returning a partial answer."""
    params = {
        "supplierNumber": supplier_number,
        "lviNumber": lvi_number,
        "businessID": business_id,
    }
    try:
        products, total_results, pages_fetched = await _fetch_all_pages(params, max_pages)
    except LviApiError as e:
        return {"etimClassId": etim_class_id, "matchCount": 0, "products": [], "reason": str(e)}

    matches = []
    for product in products:
        for feature in product.get("etimFeatureValues", []):
            if feature.get("etimClassId") != etim_class_id:
                continue
            if etim_feature_id and feature.get("etimFeatureId") != etim_feature_id:
                continue
            matches.append(_project(product))
            break

    result = {
        "etimClassId": etim_class_id,
        "etimFeatureId": etim_feature_id,
        "matchCount": len(matches),
        "products": matches,
        "scannedCount": len(products),
    }
    if total_results is not None and len(products) < total_results:
        result["coverageNote"] = (
            f"Scanned {len(products)} of {total_results} candidate product(s) across "
            f"{pages_fetched} page(s) before stopping; pass supplier_number/business_id/"
            f"lvi_number to narrow the candidate set for full coverage."
        )
    return result


@mcp.tool
async def list_suppliers() -> dict:
    """List every supplier the configured API key can see."""
    try:
        data = await _get_json(SUPPLIERS_PATH, {})
    except LviApiError as e:
        return {"suppliers": [], "reason": str(e)}
    return {"suppliers": data or []}


@mcp.tool
async def get_changed_products(
    last_modified_after: str | None = None,
    last_modified_before: str | None = None,
) -> dict:
    """List LVI numbers that changed within an optional date range."""
    try:
        data = await _get_json(
            CHANGED_PATH,
            {"lastModifiedAfter": last_modified_after, "lastModifiedBefore": last_modified_before},
        )
    except LviApiError as e:
        return {"changed": [], "reason": str(e)}
    return {"changed": data or []}


if __name__ == "__main__":
    mcp.run()
