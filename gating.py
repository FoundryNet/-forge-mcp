"""Per-client API gating for the FoundryNet MCP server.

Until now every client of foundrynet-mcp-production shared one upstream
FOUNDRYNET_API_KEY. This module flips that: each MCP client presents its
own `Authorization: Bearer fnet_…` header on every tool call, we validate
the key against Supabase, look up its tier, gate the requested tool
against a tier→tools allowlist, and atomically check+increment a monthly
usage counter against the per-tier cap.

Two tiers (Enterprise + attest_machine_action are deferred):
  • Free  — 7 tools (read-only + fire_sandbox + corpus-feedback +
            coverage introspection), 100 calls/month. Free keys are minted
            self-serve via /v1/keys (no Stripe binding); `free_tier=true`
            in forge_api_keys. Demo (chat-minted) keys also fall here.
  • Pro   — all 14 deployed tools, 10 000 calls/month. Any active
            forge_api_keys row without `free_tier=true` (i.e. paid keys
            with a Stripe subscription) qualifies as Pro.

Errors are surfaced via `fastmcp.exceptions.ToolError`, which the MCP
wire layer translates to `isError=true` with the error message as the
content text. The message body is a JSON string carrying:
  {error, message, current_tier?, tool?, call_count?, cap?, upgrade_url, signup_url}
so any MCP client / LLM can read the structured fields and route the user
to the right URL.

Failure modes never produce 500s — Supabase blips fail OPEN (allow the
call but log) rather than 503'ing a paying customer's workflow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers
from supabase import create_client, Client

logger = logging.getLogger("foundrynet.mcp.gating")

# ── Tier → tools (existing tool names; spec's `normalize_field` etc. map
#    1:1 by intent — see error messages and README for the spec↔code map).
FREE_TOOLS: frozenset[str] = frozenset({
    "normalize_telemetry",     # spec: normalize_field
    "query_machine_history",   # spec: query_machine_data + get_machine_history
    "list_automations",        # spec: get_integrations (lists configured automations)
    "query_webhook_history",   # spec: get_integrations (delivery / status)
    "fire_sandbox",            # demo-to-conversion unlock: full loop, 10 fires/key lifetime
    "correct_mapping",         # corpus-improvement signal — free so every interaction trains the corpus
    "get_coverage",            # pre-flight schema introspection — free so agents can check before trying
})
ALL_TOOLS: frozenset[str] = FREE_TOOLS | frozenset({
    "identify_machine",        # spec: register_machine (Pro)
    "create_automation",       # spec: create_trigger (Pro)
    "activate_automation",     # spec: execute_action (Pro)
    "disable_automation",
    "delete_automation",
    "restore_automation",
    "verify_on_chain",         # spec: settle_machine_work (Pro)
})

FREE_CAP = int(os.environ.get("MCP_FREE_CAP", "100"))
PRO_CAP  = int(os.environ.get("MCP_PRO_CAP",  "10000"))

# fire_sandbox is the demo-to-conversion unlock — it's in FREE_TOOLS so any
# valid key can call it, but it carries its own lifetime cap (not monthly)
# so a single key can't pin the sandbox endpoint forever. Stored in the
# same forge_mcp_usage table using month='sandbox-lifetime' so the existing
# atomic check+increment RPC works unchanged.
SANDBOX_FIRE_CAP   = int(os.environ.get("MCP_SANDBOX_FIRE_CAP", "10"))
SANDBOX_MONTH_KEY  = "sandbox-lifetime"

PRICING_URL = os.environ.get("MCP_PRICING_URL", "https://foundrynet.io/pricing")
SIGNUP_URL  = os.environ.get("MCP_SIGNUP_URL",  "https://foundrynet.io/signup")

API_KEY_PREFIX = "fnet_"

_client: Optional[Client] = None


def _supabase() -> Optional[Client]:
    """Lazy Supabase client. Returns None if credentials aren't configured;
    callers MUST treat that as fail-open so the server still works as
    today during the rollout window before SUPABASE_* env vars land."""
    global _client
    if _client is not None:
        return _client
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not (url and key):
        logger.warning("gating: SUPABASE_URL/SUPABASE_SERVICE_KEY not set — gating disabled (fail open)")
        return None
    try:
        _client = create_client(url, key)
    except Exception as e:
        logger.warning(f"gating: Supabase client init failed: {type(e).__name__}: {e}")
        return None
    return _client


def _hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _current_month_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ── Key validation + tier resolution ─────────────────────────────────────────

def validate_key(token: str) -> Optional[dict]:
    """Return {user_id, tier, key_id} on a hit, None on any miss.

    tier == 'free' when the key was minted with free_tier=true OR is_demo=true;
    tier == 'pro' otherwise (paid key, has a Stripe subscription).

    Mirrors forge-prod/api.py's `_lookup_api_key_user` SHA256 + active-row
    scheme so a single key works against both Forge API and the MCP server.
    """
    token = (token or "").strip()
    if not token.startswith(API_KEY_PREFIX):
        return None
    client = _supabase()
    if client is None:
        return None
    try:
        h = _hash_api_key(token)
        r = (client.table("forge_api_keys")
             .select("id,user_id,status,is_demo,free_tier,stripe_subscription_id")
             .eq("key_hash", h)
             .eq("status", "active")
             .limit(1)
             .execute())
    except Exception as e:
        logger.warning(f"gating: validate_key Supabase failure: {type(e).__name__}: {e}")
        return None
    rows = r.data or []
    if not rows:
        return None
    row = rows[0]
    tier = "free" if (row.get("free_tier") or row.get("is_demo")) else "pro"
    return {"user_id": row["user_id"], "tier": tier, "key_id": row["id"]}


# ── Rate-limit counter (atomic RPC) ──────────────────────────────────────────

def check_and_increment(user_id: str, tier: str) -> tuple[bool, int, int]:
    """Atomically check the monthly cap, then increment if under it.
    Returns (allowed, new_call_count, cap). Fails OPEN on Supabase errors
    so a momentary outage never breaks paying clients."""
    cap = PRO_CAP if tier == "pro" else FREE_CAP
    client = _supabase()
    if client is None:
        return (True, 0, cap)
    try:
        r = client.rpc("increment_mcp_usage", {
            "p_user_id": user_id,
            "p_month":   _current_month_utc(),
            "p_cap":     cap,
        }).execute()
    except Exception as e:
        logger.warning(f"gating: increment_mcp_usage RPC failed: {type(e).__name__}: {e}")
        return (True, 0, cap)
    rows = r.data or []
    if not rows:
        return (True, 0, cap)
    row = rows[0]
    return (bool(row.get("allowed")), int(row.get("call_count") or 0), int(row.get("cap") or cap))


# ── Error payloads (JSON-encoded into ToolError messages) ────────────────────

def _err(payload: dict) -> ToolError:
    return ToolError(json.dumps(payload))


def _err_no_auth() -> ToolError:
    return _err({
        "error":      "missing_api_key",
        "message":    f"Authorization: Bearer fnet_… required. Sign up free at {SIGNUP_URL}",
        "signup_url": SIGNUP_URL,
    })


def _err_invalid_key() -> ToolError:
    return _err({
        "error":      "invalid_api_key",
        "message":    f"API key not recognized or revoked. Mint a new key at {SIGNUP_URL}",
        "signup_url": SIGNUP_URL,
    })


def _err_tier(tool_name: str, tier: str) -> ToolError:
    msg = (
        f"{tool_name} requires Forge Pro. Try `fire_sandbox` for free "
        f"to see the full watch→fire→settle loop (10 lifetime fires per key, "
        f"no card), or upgrade for unlimited at {PRICING_URL}"
    )
    return _err({
        "error":         "tool_requires_upgrade",
        "message":       msg,
        "tool":          tool_name,
        "current_tier":  tier,
        "free_demo":     "fire_sandbox",
        "upgrade_url":   PRICING_URL,
    })


def _err_sandbox_exhausted(count: int) -> ToolError:
    return _err({
        "error":        "sandbox_cap_reached",
        "message":      (f"fire_sandbox cap reached ({count}/{SANDBOX_FIRE_CAP} lifetime fires "
                         f"on this key). The full loop is unlimited on Pro — {PRICING_URL}"),
        "tool":         "fire_sandbox",
        "call_count":   count,
        "cap":          SANDBOX_FIRE_CAP,
        "upgrade_url":  PRICING_URL,
    })


def _err_rate(tier: str, count: int, cap: int) -> ToolError:
    return _err({
        "error":        "rate_limit_exceeded",
        "message":      (f"Monthly cap of {cap:,} tool calls reached on {tier} tier. "
                         f"Upgrade at {PRICING_URL}"),
        "current_tier": tier,
        "call_count":   count,
        "cap":          cap,
        "upgrade_url":  PRICING_URL,
    })


# ── The middleware itself ────────────────────────────────────────────────────

class GatingMiddleware(Middleware):
    """fastmcp middleware: gates every tool call by tier + monthly cap.

    on_call_tool runs once per `tools/call`. We pull headers via
    `get_http_headers()` (works for SSE because each client message is its
    own HTTP request that carries the Authorization header), validate the
    fnet_ key, then either allow the call or raise ToolError with the
    structured payload the client/LLM can parse.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        # Tool name lives on the inbound message; defensive defaults so a
        # malformed request still produces a clean structured error.
        tool_name = getattr(context.message, "name", None) or ""

        # IMPORTANT: get_http_headers() strips `authorization` by default
        # (it's in fastmcp's excluded-headers set, treated as "problematic for
        # proxy forwarding"). We need it as the gate's only input, so opt back
        # in via the `include` set.
        headers = get_http_headers(include={"authorization"}) or {}
        auth = (headers.get("authorization") or "").strip()
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        if not token:
            raise _err_no_auth()

        info = validate_key(token)
        if info is None:
            raise _err_invalid_key()

        tier = info["tier"]
        allowed_tools = ALL_TOOLS if tier == "pro" else FREE_TOOLS
        if tool_name not in allowed_tools:
            raise _err_tier(tool_name, tier)

        # fire_sandbox runs against a separate lifetime counter (not the
        # monthly one) so a Free key can demo the full loop without burning
        # its month's allowance, and so the sandbox endpoint can't be
        # held open by a single key forever.
        if tool_name == "fire_sandbox":
            client = _supabase()
            if client is not None:
                try:
                    r = client.rpc("increment_mcp_usage", {
                        "p_user_id": info["user_id"],
                        "p_month":   SANDBOX_MONTH_KEY,
                        "p_cap":     SANDBOX_FIRE_CAP,
                    }).execute()
                    rows = r.data or []
                    if rows:
                        row = rows[0]
                        if not bool(row.get("allowed")):
                            raise _err_sandbox_exhausted(int(row.get("call_count") or 0))
                except ToolError:
                    raise
                except Exception as e:
                    logger.warning(f"gating: sandbox counter failed: {type(e).__name__}: {e}")
                    # Fail OPEN — same posture as the monthly counter.
        else:
            ok, count, cap = check_and_increment(info["user_id"], tier)
            if not ok:
                raise _err_rate(tier, count, cap)

        # Stash tier so tools could surface it via meta if useful later.
        try:
            ctx = getattr(context, "fastmcp_context", None)
            if ctx is not None:
                # Best-effort: keep the key off the request for any downstream
                # tool that wants to log the caller without re-validating.
                ctx.state = getattr(ctx, "state", {}) or {}
                ctx.state.update({
                    "mcp_tier":       tier,
                    "mcp_user_id":    info["user_id"],
                    "mcp_call_count": count,
                    "mcp_cap":        cap,
                })
        except Exception:
            pass

        return await call_next(context)
