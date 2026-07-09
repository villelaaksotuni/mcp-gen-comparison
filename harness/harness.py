#!/usr/bin/env python3
"""Shared harness for mcp-gen-comparison.

Grades a *generated MCP server* (an "arm") against one fixed rubric, so every
arm is compared identically. Chain under test: harness -> arm's MCP server
(stdio) -> LVI-INFO Read-API. See CLAUDE.md for the full rubric and golden
rules — this file must never be tuned to any one arm's output.

Two modes:
  --selfcheck --fixture <path>   offline: proves the harness's own logic
                                  against a captured fixture, no server, no key.
  --arm <name> --server <path>   live: launches arms/<arm>/server.py over
                                  stdio and runs the full rubric against it.
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

sys.path.insert(0, str(Path(__file__).resolve().parent))
import expected  # noqa: E402 (needs the sys.path tweak above first)

CHECK_NAMES = [
    "lookup_by_lvi",
    "lookup_by_link",
    "supplier_filter",
    "etim_filter",
    "projection",
    "pagesize_clamp",
    "pagination_ceiling",
    "bad_input",
    "empty_vs_error",
    "auth_fail",
]

PROJECTION_MAX_FIELDS = 12

NOT_FOUND_WORDS = re.compile(r"not found|no product|no match|does ?n[o']t exist", re.I)
NO_MATCHES_WORDS = re.compile(r"no match|no product|no result|0 result|zero result|empty", re.I)
AUTH_WORDS = re.compile(r"auth|api ?key|unauthori[sz]ed|forbidden|invalid key|401|403", re.I)

LEAK_SUBSTRINGS = (
    "Traceback (most recent call last)",
    "httpx.",
    "all shards failed",
    "requests.exceptions",
    "ConnectionError",
    "JSONDecodeError",
)

NO_BINDING = object()  # sentinel: this arm has no tool mapped for a check


# --------------------------------------------------------------------------
# Generic helpers — recursively inspect whatever shape a tool call returned.
# Arms will not agree on response shape, so checks search for the expected
# data anywhere in the structure rather than assuming one fixed layout.
# --------------------------------------------------------------------------

def iter_dicts(obj):
    """Yield every dict found anywhere inside a nested list/dict structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts(item)


def find_dict_with_field(obj, field, value=None):
    """First dict anywhere in obj that has `field` (and, if given, `field == value`)."""
    for d in iter_dicts(obj):
        if field in d and (value is None or str(d[field]) == str(value)):
            return d
    return None


def find_all_dicts_with_field(obj, field):
    return [d for d in iter_dicts(obj) if field in d]


def find_etim_products(products, etim_class_id, etim_feature_id=None):
    """TT020 of every product carrying the given ETIM class (and, if given,
    feature). The API has no query param for this — it's in-memory-only,
    which is exactly what this helper (and the etim_filter check) exercises."""
    matches = []
    for p in products:
        for f in p.get("etimFeatureValues", []):
            if f.get("etimClassId") != etim_class_id:
                continue
            if etim_feature_id and f.get("etimFeatureId") != etim_feature_id:
                continue
            matches.append(p.get("TT020"))
            break
    return matches


def payload(result):
    """Structured data from a tool call: prefer fastmcp's parsed `.data`,
    fall back to the raw structured content dict."""
    if result.data is not None:
        return result.data
    return result.structured_content


def text_blob(result):
    """Every string a tool call produced, joined for substring/regex checks."""
    parts = [getattr(c, "text", "") for c in result.content]
    if result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, default=str, ensure_ascii=False))
    if result.data is not None:
        parts.append(json.dumps(result.data, default=str, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def looks_like_leaked_error(text):
    """True if raw exception text or the raw LVI-INFO error envelope
    ({timestamp, status, error, path}) leaked through to the caller."""
    if any(s in text for s in LEAK_SUBSTRINGS):
        return True
    lowered = text.lower()
    envelope_keys = ("timestamp", "status", "error", "path")
    return sum(k in lowered for k in envelope_keys) >= 3


# --------------------------------------------------------------------------
# Rubric checks. Each takes the outcome of calling the bound tool — a
# CallToolResult, the NO_BINDING sentinel, or an Exception (transport crash)
# — and returns (status, reason) with status in {"PASS", "FAIL", "NA"}.
# --------------------------------------------------------------------------

def validate_lookup_by_lvi(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    product = find_dict_with_field(payload(result), "TT020", expected.LVI_NUMBER)
    if product:
        return "PASS", f"found product with TT020=={expected.LVI_NUMBER}"
    return "FAIL", f"no product with TT020=={expected.LVI_NUMBER} in response"


def validate_lookup_by_link(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    product = find_dict_with_field(payload(result), "TT020", expected.LVI_NUMBER)
    if product:
        return "PASS", f"lookup by link {expected.PRODUCT_LINK_NUMBER} resolved to TT020=={expected.LVI_NUMBER}"
    return "FAIL", f"no product with TT020=={expected.LVI_NUMBER} in response"


def validate_supplier_filter(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    products = find_all_dicts_with_field(payload(result), "TT024")
    if not products:
        return "FAIL", "no products with a TT024 (supplier) field in response"
    bad = [p["TT024"] for p in products if not str(p["TT024"]).endswith(expected.SUPPLIER_NUMBER)]
    if bad:
        return "FAIL", f"{len(bad)} product(s) with TT024 not ending in {expected.SUPPLIER_NUMBER}, e.g. {bad[0]}"
    return "PASS", f"{len(products)} product(s) returned, all TT024 end in {expected.SUPPLIER_NUMBER}"


def validate_etim_filter(result):
    if result is NO_BINDING:
        return "FAIL", "no ETIM tool bound for this arm"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    blob = text_blob(result)
    missing = [p for p in expected.ETIM_PRODUCTS if p not in blob]
    if missing:
        return "FAIL", f"response is missing expected ETIM product(s) {missing}"
    return "PASS", f"response contains {expected.ETIM_PRODUCTS}"


def validate_projection(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    product = find_dict_with_field(payload(result), "TT020", expected.LVI_NUMBER)
    if not product:
        return "FAIL", "could not locate the product object in the response"
    n = len(product)
    if n <= PROJECTION_MAX_FIELDS:
        return "PASS", f"product object has {n} fields (<= {PROJECTION_MAX_FIELDS})"
    return "FAIL", f"product object has {n} fields — raw dump, not trimmed"


def validate_pagesize_clamp(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    blob = text_blob(result)
    if looks_like_leaked_error(blob):
        return "FAIL", "raw API/exception error leaked to caller"
    if result.is_error:
        return "PASS", "oversized pageSize rejected cleanly"
    products = find_all_dicts_with_field(payload(result), "TT020")
    if len(products) > expected.API_MAX_PAGE_SIZE:
        return "FAIL", f"returned {len(products)} products — not clamped to {expected.API_MAX_PAGE_SIZE}"
    return "PASS", "no crash, no raw error leak, result size within API max"


def validate_pagination_ceiling(result):
    if result is NO_BINDING:
        return "NA", "no supplier >15000 bound for this arm; not a fair test on supplier 8293 (~10000)"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    blob = text_blob(result)
    if looks_like_leaked_error(blob):
        return "FAIL", "raw 500 'all shards failed' (or similar) leaked to caller"
    return "PASS", "deep pagination handled without a raw error leak"


def validate_bad_input(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    blob = text_blob(result)
    if looks_like_leaked_error(blob):
        return "FAIL", "raw error leaked to caller"
    product = find_dict_with_field(payload(result), "TT020")
    if product:
        return "FAIL", f"server returned a product (TT020={product.get('TT020')}) for a nonexistent LVI-number"
    if result.is_error or NOT_FOUND_WORDS.search(blob):
        return "PASS", "clean 'not found' signal"
    return "FAIL", "no product returned, but no clear 'not found' signal either"


def validate_empty_vs_error(result):
    if result is NO_BINDING:
        return "FAIL", "no tool bound for this check"
    if isinstance(result, Exception):
        return "FAIL", f"transport crash: {result}"
    blob = text_blob(result)
    if looks_like_leaked_error(blob):
        return "FAIL", "raw error leaked to caller"
    products = find_all_dicts_with_field(payload(result), "TT020")
    if products:
        return "FAIL", f"expected zero matches, got {len(products)} product(s)"
    if NO_MATCHES_WORDS.search(blob):
        return "PASS", "clear 'no matches' signal"
    return "FAIL", "raw empty envelope with no explanation"


async def check_auth_fail(server_path, base_env, binding):
    """Needs its own subprocess: relaunches the server with a deliberately
    bad LVI_API_KEY and checks the auth failure is surfaced cleanly."""
    b = binding.get("auth_fail")
    if not b or not b.get("tool"):
        return "FAIL", "no tool bound for this check"

    bad_env = dict(base_env)
    bad_env["LVI_API_KEY"] = "deliberately-invalid-key-for-harness-testing"
    transport = PythonStdioTransport(script_path=server_path, env=bad_env)

    try:
        async with Client(transport) as client:
            result = await client.call_tool(b["tool"], b.get("args", {}), raise_on_error=False)
    except Exception as e:
        text = str(e)
        if looks_like_leaked_error(text):
            return "FAIL", f"raw error leaked at transport level: {text[:200]}"
        if AUTH_WORDS.search(text):
            return "PASS", f"clean auth error at transport level: {text[:200]}"
        return "FAIL", f"server/transport failed without a recognizable auth error: {text[:200]}"

    blob = text_blob(result)
    if looks_like_leaked_error(blob):
        return "FAIL", "raw 403 envelope leaked to caller"
    if AUTH_WORDS.search(blob):
        return "PASS", "clean auth error surfaced"
    return "FAIL", "no clear auth-failure signal in response"


VALIDATORS = {
    "lookup_by_lvi": validate_lookup_by_lvi,
    "lookup_by_link": validate_lookup_by_link,
    "supplier_filter": validate_supplier_filter,
    "etim_filter": validate_etim_filter,
    "projection": validate_projection,
    "pagesize_clamp": validate_pagesize_clamp,
    "pagination_ceiling": validate_pagination_ceiling,
    "bad_input": validate_bad_input,
    "empty_vs_error": validate_empty_vs_error,
}


# --------------------------------------------------------------------------
# Selfcheck mode — proves the harness's own logic against the fixture,
# with no server and no API key involved.
# --------------------------------------------------------------------------

def report_selfcheck(name, ok, detail):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}\n         {detail}")
    return ok


def run_selfcheck(fixture_path):
    print(f"=== selfcheck against {fixture_path} ===\n")
    data = json.loads(Path(fixture_path).read_text())
    products = data["products"]
    ok = True

    by_class = find_etim_products(products, expected.ETIM_CLASS_ID)
    by_class_and_feature = find_etim_products(products, expected.ETIM_CLASS_ID, expected.ETIM_FEATURE_ID)
    check = set(expected.ETIM_PRODUCTS) <= set(by_class) and set(expected.ETIM_PRODUCTS) <= set(by_class_and_feature)
    ok &= report_selfcheck(
        "etim_helper_finds_discriminator_products",
        check,
        f"class-only matches={by_class}, class+feature matches={by_class_and_feature}",
    )

    product = find_dict_with_field(products, "TT020", expected.LVI_NUMBER)
    check = product is not None and product.get("TT024", "").endswith(expected.SUPPLIER_NUMBER)
    ok &= report_selfcheck(
        "find_dict_with_field_locates_known_product",
        check,
        f"found={product is not None}, TT024={product.get('TT024') if product else None}",
    )

    all_supplier = find_all_dicts_with_field(products, "TT024")
    check = len(all_supplier) == len(products) and all(
        p["TT024"].endswith(expected.SUPPLIER_NUMBER) for p in all_supplier
    )
    ok &= report_selfcheck(
        "find_all_dicts_with_field_matches_every_product",
        check,
        f"{len(all_supplier)}/{len(products)} products carry TT024 ending in {expected.SUPPLIER_NUMBER}",
    )

    raw_field_count = len(product)
    trimmed = {k: product[k] for k in list(product)[:8]}
    check = raw_field_count > PROJECTION_MAX_FIELDS and len(trimmed) <= PROJECTION_MAX_FIELDS
    ok &= report_selfcheck(
        "projection_field_counter_distinguishes_raw_vs_trimmed",
        check,
        f"raw product has {raw_field_count} fields, synthetic trimmed has {len(trimmed)}",
    )

    empty_envelope = {"page": 0, "totalResults": 0, "totalPages": 0, "products": []}
    check = find_all_dicts_with_field(empty_envelope, "TT020") == []
    ok &= report_selfcheck(
        "empty_detection_finds_no_products_in_empty_envelope",
        check,
        "find_all_dicts_with_field returned [] for a zero-result envelope",
    )

    missing = find_dict_with_field(products, "TT020", expected.BAD_LVI_NUMBER)
    check = missing is None
    ok &= report_selfcheck(
        "bad_input_lvi_number_absent_from_fixture",
        check,
        f"lookup for {expected.BAD_LVI_NUMBER} correctly found nothing",
    )

    print()
    print("SELFCHECK PASSED — harness logic is sound, safe to grade a live arm.\n" if ok
          else "SELFCHECK FAILED — fix the harness before grading any arm.\n")
    return bool(ok)


# --------------------------------------------------------------------------
# Live grading mode.
# --------------------------------------------------------------------------

def load_binding(arm):
    path = Path(f"arms/{arm}/binding.json")
    if not path.exists():
        return {}
    return json.loads(path.read_text())


async def grade_arm(arm, server_path):
    binding = load_binding(arm)
    env = os.environ.copy()
    transport = PythonStdioTransport(script_path=server_path, env=env)
    outcomes = {}

    async with Client(transport) as client:
        tools = await client.list_tools()
        tool_names = sorted(t.name for t in tools)

        async def run(name):
            b = binding.get(name)
            if not b or not b.get("tool"):
                return NO_BINDING
            try:
                return await client.call_tool(b["tool"], b.get("args", {}), raise_on_error=False)
            except Exception as e:
                return e

        for name, validator in VALIDATORS.items():
            outcomes[name] = validator(await run(name))

    outcomes["auth_fail"] = await check_auth_fail(server_path, env, binding)
    return tool_names, outcomes


def print_report(arm, tool_names, outcomes):
    print(f"\n=== {arm} ===")
    print(f"tools ({len(tool_names)}): {', '.join(tool_names) or '(none)'}\n")
    for name in CHECK_NAMES:
        status, reason = outcomes[name]
        print(f"  [{status:4}] {name:20} {reason}")
    total = sum(1 for name in CHECK_NAMES if outcomes[name][0] == "PASS")
    print(f"\n{total}/{len(CHECK_NAMES)} checks passed\n")


def append_csv_row(arm, tool_names, outcomes):
    results_path = Path("results/results.csv")
    results_path.parent.mkdir(exist_ok=True)
    is_new = not results_path.exists()
    with results_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["arm", "timestamp", "n_tools", "tool_names"] + CHECK_NAMES + ["total_passed"])
        statuses = [outcomes[name][0] for name in CHECK_NAMES]
        total_passed = statuses.count("PASS")
        writer.writerow(
            [arm, datetime.now().isoformat(timespec="seconds"), len(tool_names), ";".join(tool_names)]
            + statuses
            + [total_passed]
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selfcheck", action="store_true", help="validate the harness's own logic (offline)")
    parser.add_argument("--fixture", default=expected.FIXTURE_PATH, help="fixture path for --selfcheck")
    parser.add_argument("--arm", help="arm name; loads arms/<arm>/binding.json, labels results.csv")
    parser.add_argument("--server", help="path to the arm's server.py entry point")
    args = parser.parse_args()

    if args.selfcheck:
        sys.exit(0 if run_selfcheck(args.fixture) else 1)

    if not args.arm or not args.server:
        parser.error("--arm and --server are required unless --selfcheck is given")

    load_dotenv()
    if not os.environ.get("LVI_API_KEY"):
        parser.error("LVI_API_KEY is not set (check your .env)")

    tool_names, outcomes = asyncio.run(grade_arm(args.arm, args.server))
    print_report(args.arm, tool_names, outcomes)
    append_csv_row(args.arm, tool_names, outcomes)


if __name__ == "__main__":
    main()
