"""MCP server exposing the Finnish LVI-INFO Read-API (a building-products catalog).

Read-only. All requests hit LVI_API_BASE with LVI_API_KEY as a query parameter,
both read from the environment. See CLAUDE.md and openapi.json for API details.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

LVI_API_BASE = os.environ.get("LVI_API_BASE", "").rstrip("/")
LVI_API_KEY = os.environ.get("LVI_API_KEY", "")

# The API returns HTTP 500 "all shards failed" once page*pageSize crosses ~15000.
# We warn before hitting it so the agent can narrow the query instead of guessing why it failed.
MAX_RESULT_INDEX = 15000

mcp = FastMCP(
    name="lvi-info",
    instructions=(
        "Look up and search building products (plumbing/HVAC/electrical, 'LVI') in the "
        "Finnish LVI-INFO catalog. Product identity: LVI number (TT020) is the manufacturer-level "
        "number; productLinkNumber (TT024) uniquely identifies a specific supplier's listing of a "
        "product and is what get_product() expects. GTIN-13 (TT052) is the barcode, when present. "
        "The API has no free-text search -- use search_products_by_keyword for description/name "
        "matching, or list_products for exact structured filtering."
    ),
)


def _require_config() -> None:
    if not LVI_API_KEY:
        raise ToolError("LVI_API_KEY environment variable is not set.")
    if not LVI_API_BASE:
        raise ToolError("LVI_API_BASE environment variable is not set.")


def _raise_for_response(resp: httpx.Response, *, context: str) -> None:
    if resp.status_code == 200:
        return
    if resp.status_code == 403:
        raise ToolError(
            f"LVI-INFO API rejected the API key (403 Forbidden) while {context}. "
            "Check that LVI_API_KEY is correct for this environment."
        )
    if resp.status_code >= 500:
        raise ToolError(
            f"LVI-INFO API returned a server error ({resp.status_code}) while {context}. "
            "If this was a paged full-data query, the API fails once the result index passes "
            "~15000 -- narrow the query with supplierNumber or another filter and try again."
        )
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    raise ToolError(f"LVI-INFO API error ({resp.status_code}) while {context}: {body}")


async def _get(path: str, params: dict[str, Any], *, context: str) -> Any:
    _require_config()
    query = {k: v for k, v in params.items() if v is not None}
    query["apiKey"] = LVI_API_KEY
    async with httpx.AsyncClient(base_url=LVI_API_BASE, timeout=30.0) as client:
        resp = await client.get(path, params=query)
    _raise_for_response(resp, context=context)
    return resp.json()


def _reshape_etim_features(values: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten each etimFeatureValues entry into a simpler shape.

    No metadata endpoint exists to decode etimFeatureId/etimClassId to human labels,
    so the raw codes are kept -- only the envelope is simplified.
    """
    reshaped = []
    for v in values or []:
        reshaped.append(
            {
                "class_id": v.get("etimClassId"),
                "feature_id": v.get("etimFeatureId"),
                "feature_type": v.get("etimFeatureType"),
                "value1": v.get("value1"),
                "value2": v.get("value2"),
                "unit": v.get("unitOfMeasureAbbreviation"),
                "unit_id": v.get("etimUnitOfMeasureId"),
            }
        )
    return reshaped


def _reshape_product(product: dict[str, Any]) -> dict[str, Any]:
    reshaped = dict(product)
    if "etimFeatureValues" in reshaped:
        reshaped["etimFeatureValues"] = _reshape_etim_features(reshaped["etimFeatureValues"])
    return reshaped


def _check_result_index(page: int, page_size: int) -> None:
    if (page + 1) * page_size > MAX_RESULT_INDEX:
        raise ToolError(
            f"Requesting page {page} at pageSize {page_size} would read past the API's "
            f"~{MAX_RESULT_INDEX}-result limit (it returns HTTP 500 'all shards failed' beyond "
            "that point). Narrow the query with supplierNumber (or another filter) to reduce the "
            "result set, then page through that instead."
        )


@mcp.tool
async def get_product(product_link_number: str) -> dict[str, Any]:
    """Fetch a single product by its LVI product-link number (TT024).

    productLinkNumber uniquely identifies one supplier's listing of a product
    (built from the LVI number + supplier customer number, 11 characters).
    Use this when you already have that identifier, e.g. from list_products
    or search_products_by_keyword results.
    """
    data = await _get(
        f"/api/v1/products/full-data/{product_link_number}",
        {},
        context=f"fetching product {product_link_number}",
    )
    return _reshape_product(data)


@mcp.tool
async def get_product_by_gtin(gtin: str) -> dict[str, Any]:
    """Fetch a single product by its GTIN-13 barcode. Only returns active products."""
    data = await _get(
        f"/api/v1/products/full-data/gtin/{gtin}",
        {},
        context=f"fetching product by GTIN {gtin}",
    )
    return _reshape_product(data)


@mcp.tool
async def list_products(
    page: int = 0,
    page_size: int = 50,
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
) -> dict[str, Any]:
    """List/filter products from the catalog. Returns {page, totalResults, totalPages, products}.

    All filters are optional and combine with AND. If nothing matches, this returns
    an empty products list with totalResults=0 -- that's a normal "no matches" result,
    not an error.

    Args:
        page: Zero-based page number.
        page_size: Results per page, 1-1000.
        supplier_number: Supplier's 4-digit LVI-INFO customer number.
        lvi_number: Product's LVI number. Union products (Y) can return multiple matches.
        business_id: Supplier's Finnish business ID (Y-tunnus), e.g. "1234567-8".
        product_type: "M" for a normal product, "Y" for a union/group product.
        has_attachments: Filter to products that do/don't have attachments.
        is_active: Filter to active or deleted/deactivated products.
        published_date_after: Products first published on/after this date (yyyy-MM-dd).
        published_date_before: Products first published strictly before this date (yyyy-MM-dd).
        last_modified_after: Products last modified on/after this date (yyyy-MM-dd).
        last_modified_before: Products last modified strictly before this date (yyyy-MM-dd).
    """
    _check_result_index(page, page_size)
    data = await _get(
        "/api/v1/products/full-data",
        {
            "page": page,
            "pageSize": page_size,
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
        },
        context="listing products",
    )
    data["products"] = [_reshape_product(p) for p in data.get("products", [])]
    return data


@mcp.tool
async def list_suppliers() -> list[dict[str, Any]]:
    """List the suppliers visible to the configured API key (name, supplierNumber, vatNumber)."""
    return await _get("/api/v1/products/meta/suppliers", {}, context="listing suppliers")


@mcp.tool
async def get_changed_products(
    last_modified_after: str | None = None,
    last_modified_before: str | None = None,
) -> list[str]:
    """Get LVI-numbers of products changed in a date range (yyyy-MM-dd, both optional).

    Useful for sync/polling workflows: find what changed, then fetch details for
    the ones you care about with list_products(lvi_number=...).
    """
    return await _get(
        "/api/v1/products/changed",
        {
            "lastModifiedAfter": last_modified_after,
            "lastModifiedBefore": last_modified_before,
        },
        context="fetching changed products",
    )


def _product_text_blob(product: dict[str, Any]) -> str:
    fields = ("TT200", "TT201", "TT202", "TT203", "TT100", "TT110", "TT120")
    return " ".join(str(product[f]) for f in fields if product.get(f))


@mcp.tool
async def search_products_by_keyword(
    keyword: str,
    supplier_number: str | None = None,
    product_type: str | None = None,
    is_active: bool | None = True,
    max_pages: int = 5,
    page_size: int = 200,
) -> dict[str, Any]:
    """Search for products by name/description keyword (case-insensitive substring match).

    The LVI-INFO API has no free-text search parameter, so this fetches pages of
    list_products and filters locally against each product's name/description fields
    (TT200 Yleisnimi, TT201 Tekninen nimi, TT202/TT203 pitkä nimi/kuvaus, plus supplier
    and product-series names). Strongly recommend passing supplier_number to keep this
    fast and avoid scanning the whole catalog; without it, results may be incomplete
    for common keywords since only max_pages*page_size products are scanned.

    Returns {matches, pages_scanned, products_scanned, truncated} where truncated=true
    means max_pages was reached before scanning the full filtered result set.
    """
    if not keyword.strip():
        raise ToolError("keyword must be a non-empty string.")

    needle = keyword.strip().lower()
    matches: list[dict[str, Any]] = []
    products_scanned = 0
    pages_scanned = 0
    total_pages: int | None = None

    for page in range(max_pages):
        _check_result_index(page, page_size)
        data = await _get(
            "/api/v1/products/full-data",
            {
                "page": page,
                "pageSize": page_size,
                "supplierNumber": supplier_number,
                "productType": product_type,
                "isActive": is_active,
            },
            context="searching products by keyword",
        )
        pages_scanned += 1
        total_pages = data.get("totalPages", total_pages)
        products = data.get("products", [])
        products_scanned += len(products)
        for product in products:
            if needle in _product_text_blob(product).lower():
                matches.append(_reshape_product(product))
        if not products or (total_pages is not None and page + 1 >= total_pages):
            break

    truncated = total_pages is not None and pages_scanned < total_pages
    return {
        "matches": matches,
        "pages_scanned": pages_scanned,
        "products_scanned": products_scanned,
        "truncated": truncated,
    }


if __name__ == "__main__":
    mcp.run()
