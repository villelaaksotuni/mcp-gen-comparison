#!/usr/bin/env python3
"""from_openapi arm — deterministic baseline.

Pure FastMCP OpenAPI conversion of the pinned LVI-INFO spec (openapi.json),
default settings, no RouteMaps/ToolTransforms/renaming. Every endpoint becomes
a tool exactly as the spec describes it. The only customization is auth
wiring: apiKey is a required query param on every operation, so it is given a
default (the real key) in-memory and marked optional so calls don't need to
supply it, and the client injects it on every outgoing request. (FastMCP's
OpenAPI provider builds each httpx.Request directly and calls client.send()
on it, which bypasses AsyncClient's constructor-level default `params=` —
so the injection is done in an AsyncClient.send() override instead, the
equivalent wiring point for this fastmcp version.)
"""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

API_BASE = os.environ["LVI_API_BASE"]
API_KEY = os.environ["LVI_API_KEY"]

spec = json.loads((Path(__file__).parent / "openapi.json").read_text())

for path_item in spec.get("paths", {}).values():
    for operation in path_item.values():
        for param in operation.get("parameters", []):
            if param.get("name") == "apiKey":
                param["required"] = False
                param["schema"] = {**param.get("schema", {}), "default": API_KEY}


class ApiKeyInjectingClient(httpx.AsyncClient):
    async def send(self, request, **kwargs):
        request.url = request.url.copy_merge_params({"apiKey": API_KEY})
        return await super().send(request, **kwargs)


client = ApiKeyInjectingClient(base_url=API_BASE)

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    name="LVI-INFO (from_openapi baseline)",
)

if __name__ == "__main__":
    mcp.run()
