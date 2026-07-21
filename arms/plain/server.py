"""LVI-INFO MCP server.

Exposes the Finnish LVI-INFO Read-API (building-products catalog) over MCP
(stdio transport) so an AI agent can look up and search HVAC/plumbing products.

Configuration (environment variables, optionally via a .env file):
  LVI_API_KEY   — personal API key (sent as a query parameter on every request)
  LVI_API_BASE  — API base URL, e.g. https://search-api.lvi-info.fi.qa.ambientia.fi

Notes on the upstream API that shaped this server:
  * There is NO server-side free-text search — only filters (supplier, LVI
    number, GTIN, dates...). The search_products_by_keyword tool therefore
    scans catalog pages and matches text client-side.
  * totalResults is capped at 10000 by the search backend; the real catalog
    is much larger (~260k products across ~400 suppliers).
  * Paging past result index 15000 makes the backend fail with HTTP 500
    ("all shards failed"); large sets must be split by supplierNumber.
  * Products are flat records of TTxxx field codes (empty fields omitted)
    plus a nested etimFeatureValues array. This server translates the codes
    to readable English names and groups them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_dotenv_fallback() -> None:
    """If LVI_* vars are not set, look for a .env in cwd or above server.py."""
    if os.environ.get("LVI_API_KEY") and os.environ.get("LVI_API_BASE"):
        return
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    candidates += [p / ".env" for p in here.parents[:4]]
    for env_file in candidates:
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key.startswith("LVI_") and key not in os.environ:
                os.environ[key] = value
        if os.environ.get("LVI_API_KEY"):
            return


_load_dotenv_fallback()

API_BASE = os.environ.get("LVI_API_BASE", "https://search-api.lvi-info.fi.qa.ambientia.fi").rstrip("/")

# The backend caps reported totals at 10000 and errors out past index 15000.
TOTAL_RESULTS_CAP = 10_000
MAX_RESULT_INDEX = 15_000
SCAN_PAGE_SIZE = 1000

_http: httpx.Client | None = None


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(base_url=API_BASE, timeout=60.0)
    return _http


def _api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    api_key = os.environ.get("LVI_API_KEY")
    if not api_key:
        raise ToolError(
            "LVI_API_KEY is not set. Provide it via the environment or a .env file."
        )
    query = {"apiKey": api_key}
    for key, value in (params or {}).items():
        if value is not None:
            query[key] = str(value).lower() if isinstance(value, bool) else value
    try:
        response = _client().get(path, params=query)
    except httpx.HTTPError as exc:
        raise ToolError(f"Network error calling LVI-INFO API: {exc}") from exc

    if response.status_code == 200:
        return response.json()
    if response.status_code == 403:
        raise ToolError("LVI-INFO API rejected the API key (HTTP 403). Check LVI_API_KEY.")
    if response.status_code == 404:
        raise ToolError("Not found: the LVI-INFO API has no product matching that identifier.")
    if response.status_code == 500:
        raise ToolError(
            "LVI-INFO API internal error (HTTP 500). This typically happens when paging "
            f"past result index {MAX_RESULT_INDEX}. Narrow the query (e.g. filter by "
            "supplier_number) instead of paging deeper."
        )
    raise ToolError(f"LVI-INFO API error: HTTP {response.status_code}: {response.text[:300]}")


# ---------------------------------------------------------------------------
# Field translation: TTxxx codes -> readable keys
# ---------------------------------------------------------------------------

# Compact summary used in search-result lists (keeps agent context small).
SUMMARY_FIELDS = {
    "TT020": "lvi_number",
    "TT024": "product_link_number",
    "TT052": "gtin",
    "TT200": "name",
    "TT201": "technical_name",
    "TT202": "long_name",
    "TT100": "supplier",
    "TT110": "brand",
    "TT025": "product_group",
    "TT026": "product_class",
    "TT050": "supplier_product_code",
}

# Full-detail grouping. Every group is {tt_code: readable_key}.
DETAIL_GROUPS: dict[str, dict[str, str]] = {
    "identifiers": {
        "TT020": "lvi_number",
        "TT024": "product_link_number",
        "TT052": "gtin",
        "TT050": "supplier_product_code",
        "TT021": "rsk_number_se",
        "TT022": "nrf_number_no",
        "TT023": "vvs_number_dk",
        "TT060": "etim_class",
        "TT080": "unspsc_code",
        "TT081": "scip_identifier",
        "TT502": "cn8_customs_code",
        "TT071": "hen_standards",
    },
    "classification": {
        "TT025": "product_group",
        "TT026": "product_class",
    },
    "names": {
        "TT200": "name",
        "TT201": "technical_name",
        "TT202": "long_name",
        "TT203": "description",
    },
    "supplier": {
        "TT100": "supplier_name",
        "TT101": "supplier_vat_number",
        "TT110": "brand",
        "TT120": "product_series",
    },
    "physical": {
        "TT300": "length_mm",
        "TT301": "width_mm",
        "TT302": "height_mm",
        "TT303": "weight_kg",
        "TT304": "volume_l",
        "TT501": "country_of_origin",
        "TT450": "dangerous_goods_marking",
    },
    "units_and_packaging": {
        "TT400": "usage_unit",
        "TT402": "sales_unit",
        "TT401": "conversion_factor",
        "TT620": "pricing_unit",
        "TT408": "minimum_sales_batch",
        "TT409": "reel_nominal_batch",
        "TT407": "package0_type",
        "TT438": "package0_sales_units",
        "TT439": "package0_gtin",
        "TT440": "package0_length_mm",
        "TT441": "package0_width_mm",
        "TT442": "package0_height_mm",
        "TT443": "package0_weight_kg",
        "TT444": "package0_volume_l",
        "TT403": "package1_type",
        "TT410": "package1_size",
        "TT411": "package1_gtin",
        "TT412": "package1_length_mm",
        "TT413": "package1_width_mm",
        "TT414": "package1_height_mm",
        "TT415": "package1_weight_kg",
        "TT416": "package1_volume_l",
        "TT404": "package2_type",
        "TT417": "package2_size",
        "TT418": "package2_gtin",
        "TT419": "package2_length_mm",
        "TT420": "package2_width_mm",
        "TT421": "package2_height_mm",
        "TT422": "package2_weight_kg",
        "TT423": "package2_volume_l",
        "TT405": "package3_type",
        "TT424": "package3_size",
        "TT425": "package3_gtin",
        "TT426": "package3_length_mm",
        "TT427": "package3_width_mm",
        "TT428": "package3_height_mm",
        "TT429": "package3_weight_kg",
        "TT430": "package3_volume_l",
        "TT406": "package4_pallet_type",
        "TT431": "package4_pallet_size",
        "TT432": "package4_gtin",
        "TT433": "package4_length_mm",
        "TT434": "package4_width_mm",
        "TT435": "package4_height_mm",
        "TT436": "package4_weight_kg",
        "TT437": "package4_volume_l",
    },
    "lifecycle": {
        "TT511": "published_date",
        "TT512": "last_modified_date",
        "TT510": "archived_date",
        "TT522": "replaced_by_lvi_number",
        "TT523": "replaces_lvi_number",
    },
    "related_products": {
        "TT531": "spare_part_lvi_numbers",
        "TT532": "interchangeable_lvi_numbers",
        "TT533": "optional_accessory_lvi_numbers",
    },
    "media_and_documents": {
        "TT701": "main_image_url",
        "TT702": "image2_url",
        "TT704": "line_drawing_url",
        "TT705": "dimensional_drawing_url",
        "TT706": "wiring_diagram_url",
        "TT707": "technical_drawing_url",
        "TT731": "supplier_product_page_url",
        "TT736": "installation_video_url",
        "TT737": "presentation_video_url",
        "TT738": "safety_data_sheet_link_url",
        "TT755": "installation_manual_url",
        "TT756": "user_manual_url",
        "TT758": "technical_specs_url",
        "TT759": "product_declaration_url",
        "TT760": "product_certificate_url",
        "TT761": "declaration_of_performance_url",
        "TT765": "environmental_declaration_epd_url",
        "TT766": "safety_data_sheet_url",
        "TT767": "m1_document_url",
    },
    "compliance": {
        "TT780": "ce_marking",
        "TT782": "fi_certified",
        "TT783": "rohs_compliant",
        "TT784": "reach_compliant",
    },
    "sustainability": {
        "TT790": "gwp_kg_co2e_per_kg",
        "TT791": "gwp_standard",
        "TT792": "gwp_third_party_verified",
        "TT793": "co2data_fi_class_id",
        "TT794": "emission_db_conservative_value_a1_a3",
        "TT796": "environmental_declaration_registration_id",
        "TT797": "environmental_declaration_expiry_date",
        "TT798": "gwp_reference_unit",
        "TT799": "gwp_conversion_factor_to_per_kg",
        "TT800": "gwp_use_phase_b1_b7",
        "TT801": "gwp_end_of_life_c1_c4",
        "TT802": "carbon_handprint_d1_d2",
    },
}

DATE_FIELDS = {"TT510", "TT511", "TT512", "TT797"}


def _format_date(value: Any) -> Any:
    """API returns dates as 'yyyymmdd' strings; convert to ISO 'yyyy-mm-dd'."""
    if isinstance(value, str) and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _summarize(product: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tt_code, key in SUMMARY_FIELDS.items():
        value = product.get(tt_code)
        if not _is_empty(value):
            out[key] = value
    if not _is_empty(product.get("TT510")):
        out["archived_date"] = _format_date(product["TT510"])
    if not _is_empty(product.get("TT512")):
        out["last_modified_date"] = _format_date(product["TT512"])
    return out


def _simplify_etim(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for feature in features:
        item: dict[str, Any] = {
            "feature_id": feature.get("etimFeatureId"),
            "type": feature.get("etimFeatureType"),
            "value": feature.get("value1"),
        }
        if not _is_empty(feature.get("value2")):
            item["value2"] = feature["value2"]
        if not _is_empty(feature.get("unitOfMeasureAbbreviation")):
            item["unit"] = feature["unitOfMeasureAbbreviation"]
        out.append(item)
    return out


def _full_detail(product: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for group_name, fields in DETAIL_GROUPS.items():
        group: dict[str, Any] = {}
        for tt_code, key in fields.items():
            value = product.get(tt_code)
            if _is_empty(value):
                continue
            group[key] = _format_date(value) if tt_code in DATE_FIELDS else value
        if group:
            out[group_name] = group
    out["is_archived"] = not _is_empty(product.get("TT510"))
    etim = product.get("etimFeatureValues") or []
    if etim:
        out["etim_features"] = _simplify_etim(etim)
        out["etim_features_note"] = (
            "Alphanumeric values like 'EV004441' are ETIM value codes; resolving them "
            "to labels requires the ETIM dictionary (not part of this API)."
        )
    return out


def _total_results_note(total: int) -> str | None:
    if total >= TOTAL_RESULTS_CAP:
        return (
            f"total_results is capped at {TOTAL_RESULTS_CAP} by the search backend; "
            "the actual number of matches may be higher. Narrow by supplier_number "
            "for exact counts."
        )
    return None


# ---------------------------------------------------------------------------
# MCP server & tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "LVI-INFO",
    instructions=(
        "Tools for the Finnish LVI-INFO building-products catalog (HVAC, plumbing, "
        "ventilation). Product data is mostly in Finnish. Typical flows: "
        "search_products_by_keyword or search_products to find candidates, then "
        "get_product / get_product_by_gtin for full details. Use list_suppliers to "
        "discover supplier numbers for filtering."
    ),
)


@mcp.tool
def list_suppliers() -> dict[str, Any]:
    """List all suppliers visible to this API key, with their supplier numbers.

    Supplier numbers are the main way to scope searches (the catalog holds
    ~260k products, and broad queries are capped/limited by the backend).
    """
    suppliers = _api_get("/api/v1/products/meta/suppliers")
    suppliers = sorted(suppliers, key=lambda s: (s.get("name") or "").lower())
    return {
        "count": len(suppliers),
        "suppliers": [
            {
                "supplier_number": s.get("supplierNumber"),
                "name": s.get("name"),
                "vat_number": s.get("vatNumber"),
            }
            for s in suppliers
        ],
    }


@mcp.tool
def search_products(
    supplier_number: Annotated[str | None, Field(description="LVI-INFO 4-digit supplier number (from list_suppliers)")] = None,
    lvi_number: Annotated[str | None, Field(description="7-digit LVI product number; shared numbers ('yhteisnumero') can match several suppliers' products")] = None,
    business_id: Annotated[str | None, Field(description="Supplier's Finnish business ID (Y-tunnus), e.g. '1234567-8'")] = None,
    product_type: Annotated[str | None, Field(description="'M' = brand-specific number, 'Y' = shared/generic number")] = None,
    has_attachments: Annotated[bool | None, Field(description="Only products with (true) / without (false) attachments")] = None,
    is_active: Annotated[bool | None, Field(description="Only active (true) or archived (false) products")] = None,
    published_after: Annotated[str | None, Field(description="Published on/after this date, yyyy-MM-dd")] = None,
    published_before: Annotated[str | None, Field(description="Published before this date (exclusive), yyyy-MM-dd")] = None,
    modified_after: Annotated[str | None, Field(description="Last modified on/after this date, yyyy-MM-dd")] = None,
    modified_before: Annotated[str | None, Field(description="Last modified before this date (exclusive), yyyy-MM-dd")] = None,
    page: Annotated[int, Field(ge=0, description="Zero-based page number")] = 0,
    page_size: Annotated[int, Field(ge=1, le=1000, description="Results per page")] = 25,
) -> dict[str, Any]:
    """Search/browse the catalog with the API's native filters (no free text).

    Returns compact product summaries plus pagination info. For text search by
    product name/description, use search_products_by_keyword instead. Fetch
    full details for a hit with get_product(product_link_number).

    An empty result (total_results 0) means the filters matched nothing — it
    is not an error. Paging past result index 15000 fails upstream; split
    large result sets by supplier_number.
    """
    if product_type is not None and product_type not in ("M", "Y"):
        raise ToolError("product_type must be 'M' or 'Y'.")
    if (page + 1) * page_size > MAX_RESULT_INDEX:
        raise ToolError(
            f"This page would read past result index {MAX_RESULT_INDEX}, which the "
            "upstream API cannot serve. Narrow the query (e.g. by supplier_number)."
        )
    data = _api_get(
        "/api/v1/products/full-data",
        {
            "supplierNumber": supplier_number,
            "lviNumber": lvi_number,
            "businessID": business_id,
            "productType": product_type,
            "hasAttachments": has_attachments,
            "isActive": is_active,
            "publishedDateAfter": published_after,
            "publishedDateBefore": published_before,
            "lastModifiedAfter": modified_after,
            "lastModifiedBefore": modified_before,
            "page": page,
            "pageSize": page_size,
        },
    )
    total = data.get("totalResults", 0)
    result: dict[str, Any] = {
        "page": data.get("page", page),
        "total_results": total,
        "total_pages": data.get("totalPages"),
        "products": [_summarize(p) for p in data.get("products", [])],
    }
    note = _total_results_note(total)
    if note:
        result["note"] = note
    return result


@mcp.tool
def search_products_by_keyword(
    query: Annotated[str, Field(description="Space-separated search terms; a product matches if ALL terms occur (case-insensitive) in its name, description, brand, series, product group/class or supplier code. Data is mostly Finnish — prefer Finnish terms (e.g. 'suihku', 'venttiili', 'lämmitys').")],
    supplier_number: Annotated[str | None, Field(description="Strongly recommended: limit the scan to one supplier (from list_suppliers). Without it only part of the ~260k-product catalog can be scanned.")] = None,
    is_active: Annotated[bool | None, Field(description="Filter by active status before matching (default true)")] = True,
    max_results: Annotated[int, Field(ge=1, le=200, description="Stop after this many matches")] = 30,
    max_scan: Annotated[int, Field(ge=1000, le=MAX_RESULT_INDEX, description="Max products to scan server-side (1000 per request)")] = 10_000,
) -> dict[str, Any]:
    """Free-text product search (client-side): scans catalog pages and matches text.

    The LVI-INFO API has no server-side text search, so this tool pages through
    the catalog (1000 products per request) and matches your terms locally.
    Check scan_complete in the response: if false, only part of the candidate
    set was scanned — re-run with a supplier_number filter for full coverage.
    """
    terms = [t.casefold() for t in query.split() if t.strip()]
    if not terms:
        raise ToolError("query must contain at least one search term.")

    text_fields = ("TT200", "TT201", "TT202", "TT203", "TT110", "TT120", "TT025", "TT026", "TT050")
    matches: list[dict[str, Any]] = []
    scanned = 0
    total_candidates: int | None = None
    page = 0
    scan_limit = min(max_scan, MAX_RESULT_INDEX)

    while scanned < scan_limit:
        data = _api_get(
            "/api/v1/products/full-data",
            {
                "supplierNumber": supplier_number,
                "isActive": is_active,
                "page": page,
                "pageSize": SCAN_PAGE_SIZE,
            },
        )
        if total_candidates is None:
            total_candidates = data.get("totalResults", 0)
        products = data.get("products", [])
        if not products:
            break
        for product in products:
            haystack = " ".join(
                str(product.get(f, "")) for f in text_fields
            ).casefold()
            if all(term in haystack for term in terms):
                matches.append(_summarize(product))
        scanned += len(products)
        page += 1
        if len(matches) >= max_results:
            matches = matches[:max_results]
            break
        if scanned >= (total_candidates or 0):
            break

    capped_total = (total_candidates or 0) >= TOTAL_RESULTS_CAP
    scan_complete = (
        len(matches) < max_results
        and not capped_total
        and scanned >= (total_candidates or 0)
    )
    result: dict[str, Any] = {
        "query": query,
        "match_count": len(matches),
        "matches": matches,
        "products_scanned": scanned,
        "candidate_pool": (
            f"{total_candidates}+" if capped_total else total_candidates
        ),
        "scan_complete": scan_complete,
    }
    if not scan_complete:
        if len(matches) >= max_results:
            result["note"] = "Stopped at max_results; more matches may exist."
        elif supplier_number is None:
            result["note"] = (
                "Only part of the catalog was scanned. For full coverage, pass a "
                "supplier_number (see list_suppliers) and search per supplier."
            )
        else:
            result["note"] = (
                "This supplier's catalog is larger than the scan limit; raise "
                "max_scan (up to 15000) or add more specific terms."
            )
    return result


@mcp.tool
def get_product(
    product_link_number: Annotated[str, Field(description="11-character product link number (TT024 / product_link_number from search results): LVI number + supplier number")],
) -> dict[str, Any]:
    """Get one product's full data by its unique product link number.

    Returns all available fields grouped and translated to readable names:
    identifiers, names, physical dimensions, packaging levels, lifecycle dates,
    document/image URLs, compliance flags, sustainability (GWP) data and ETIM
    features. Empty fields are omitted.
    """
    return _full_detail(_api_get(f"/api/v1/products/full-data/{product_link_number}"))


@mcp.tool
def get_product_by_gtin(
    gtin: Annotated[str, Field(description="GTIN-13 barcode identifier")],
) -> dict[str, Any]:
    """Get one product's full data by GTIN-13 barcode. Only finds active products."""
    return _full_detail(_api_get(f"/api/v1/products/full-data/gtin/{gtin}"))


@mcp.tool
def get_products_by_lvi_number(
    lvi_number: Annotated[str, Field(description="7-digit LVI number, e.g. '2934135'")],
) -> dict[str, Any]:
    """Get full data for all products carrying a given LVI number.

    A 'shared' LVI number (yhteisnumero) can be used by several suppliers, so
    this may return more than one product.
    """
    data = _api_get(
        "/api/v1/products/full-data",
        {"lviNumber": lvi_number, "pageSize": 50},
    )
    products = data.get("products", [])
    return {
        "total_results": data.get("totalResults", 0),
        "products": [_full_detail(p) for p in products],
    }


@mcp.tool
def list_changed_products(
    modified_after: Annotated[str | None, Field(description="Include changes on/after this date, yyyy-MM-dd")] = None,
    modified_before: Annotated[str | None, Field(description="Include changes before this date (exclusive), yyyy-MM-dd")] = None,
    limit: Annotated[int, Field(ge=1, le=10_000, description="Max LVI numbers to return")] = 500,
) -> dict[str, Any]:
    """List LVI numbers of products changed in a date range.

    Useful for change monitoring ('what changed last week?'). Look up details
    for a returned number with get_products_by_lvi_number.
    """
    numbers = _api_get(
        "/api/v1/products/changed",
        {"lastModifiedAfter": modified_after, "lastModifiedBefore": modified_before},
    )
    result: dict[str, Any] = {
        "total_changed": len(numbers),
        "lvi_numbers": numbers[:limit],
    }
    if len(numbers) > limit:
        result["note"] = f"Truncated to first {limit} of {len(numbers)} changed LVI numbers."
    return result


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
