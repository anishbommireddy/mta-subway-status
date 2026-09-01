"""
server.py — MTA Subway Alerts MCP server (v1)

Exposes one tool to Claude:
  - get_subway_alerts(lines): current service alerts (delays, planned
    work, reroutes, "no service" notices) for the NYC subway, optionally
    filtered to specific lines.

Run locally (stdio, for testing with `mcp dev` or Claude Desktop's
local config):
    python server.py

Run as an HTTP service (what you actually need for the Claude mobile
app / claude.ai, since those only reach remote MCP servers):
    python server.py --http
which starts a Streamable HTTP server on 0.0.0.0:$PORT.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mta_feeds import KNOWN_LINES, fetch_alerts

# FastMCP's DNS-rebinding protection defaults to only trusting
# localhost/127.0.0.1 Host headers, which 421s every request once this
# is deployed behind a real public hostname (Render, Fly, etc.) — so we
# add the deployed host explicitly. Render sets RENDER_EXTERNAL_HOSTNAME
# automatically; other hosts would need their own equivalent env var.
_allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
_external_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _external_host:
    _allowed_hosts.append(_external_host)
    _allowed_origins.append(f"https://{_external_host}")

mcp = FastMCP(
    "mta-subway-status",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


@mcp.tool()
def get_subway_alerts(lines: list[str] | None = None) -> dict:
    """
    Get current NYC subway service alerts: delays, planned repair work,
    reroutes, and lines with no service right now.

    Args:
        lines: Optional list of subway line names to check, e.g.
               ["7", "E", "M", "G"]. If omitted, returns alerts for
               every line system-wide.

    Returns:
        A dict with:
        - "checked_lines": the lines that were filtered for (or "all")
        - "alerts": list of active alerts, each with routes/effect/
          header/description
        - "lines_with_no_reported_issues": lines you asked about that
          have no active alert right now (only present if `lines` was
          given) — absence of an alert generally means normal service,
          but isn't a 100% guarantee.
    """
    if lines:
        unknown = [l for l in lines if l.upper() not in KNOWN_LINES]
        if unknown:
            return {
                "error": f"Unrecognized line(s): {unknown}. "
                         f"Known lines: {sorted(KNOWN_LINES)}"
            }

    alerts = fetch_alerts(lines=lines)

    result = {
        "checked_lines": [l.upper() for l in lines] if lines else "all",
        "alerts": alerts,
    }

    if lines:
        affected = {r for a in alerts for r in a["routes"]}
        clear = sorted(l.upper() for l in lines if l.upper() not in affected)
        result["lines_with_no_reported_issues"] = clear

    return result


if __name__ == "__main__":
    if "--http" in sys.argv:
        port = int(os.environ.get("PORT", 8000))
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
