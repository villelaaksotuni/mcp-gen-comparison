"""Frozen known-good values for the LVI-INFO Read-API, captured live from
QA supplier 8293 (Onninen). This is the harness's source of truth — every
check compares an arm's server output against these values.

Do not change these without re-capturing from the live API and updating
fixtures/onninen_8293_page0.json to match. See CLAUDE.md for the full
provenance and the frozen rubric these values feed into.
"""

# Product 0101202 == ProductLinkNumber 01012028293 (same product, two lookup keys).
LVI_NUMBER = "0101202"
LVI_NUMBER_PRODUCT_NAME = "Paineputki sg-rautaa DN 150"
LVI_NUMBER_BRAND = "ONNINEN"
PRODUCT_LINK_NUMBER = "01012028293"

SUPPLIER_NUMBER = "8293"  # Onninen; totalResults ~= 10000

# ETIM discriminator: class EC011303, feature EF006272 (nominal diameter, NUMERIC, mm).
# Both 0101202 and 0101203 carry this feature. This is the check that separates
# "mirrors the API" arms from "synthesizes new capability" arms — there is no
# API query param for ETIM, so a passing tool here did in-memory filtering.
ETIM_CLASS_ID = "EC011303"
ETIM_FEATURE_ID = "EF006272"
ETIM_PRODUCTS = ["0101202", "0101203"]

BAD_LVI_NUMBER = "9999999"  # known not to exist

FIXTURE_PATH = "fixtures/onninen_8293_page0.json"

# API hard limits, used to construct the pagesize_clamp / pagination_ceiling checks.
API_MAX_PAGE_SIZE = 1000
API_RESULT_INDEX_CEILING = 15000

# ---------------------------------------------------------------------------
# Alias sets for field-name-agnostic product detection.
# Arms may rename TT-coded fields to agent-friendly keys; the harness matches
# correct VALUES under any of these key names rather than a single hardcoded one.
# ---------------------------------------------------------------------------

# Keys whose value is the LVI number (e.g. "0101202").
LVI_NUMBER_ALIASES = ("TT020", "lvi_number", "lviNumber")

# Keys whose value is the ProductLinkNumber (e.g. "01012028293").
PRODUCT_LINK_NUMBER_ALIASES = ("TT024", "product_link_number", "productLinkNumber")

# Supplier membership: a product belongs to SUPPLIER_NUMBER if ANY rule fires:
#   SUPPLIER_ENDSWITH_ALIASES  — field value ends-in SUPPLIER_NUMBER (product link encodes it)
#   SUPPLIER_EQUALS_ALIASES    — field value == SUPPLIER_NUMBER exactly
#   SUPPLIER_TEXT_ALIASES      — field value (text) contains SUPPLIER_NUMBER
SUPPLIER_ENDSWITH_ALIASES = ("TT024", "product_link_number", "productLinkNumber")
SUPPLIER_EQUALS_ALIASES = ("supplier_number", "supplierNumber")
SUPPLIER_TEXT_ALIASES = ("TT100", "supplier")
