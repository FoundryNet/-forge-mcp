"""FoundryNet MCP Server — eleven tools wrapping the live Forge v1 API,
gated per-client by fnet_ tier (Free vs Pro) as of 2026-05-28.

Exposes industrial-machine-telemetry capabilities over the Model Context
Protocol so any compatible agent (Claude Desktop, Cursor, etc.) can:
  - identify a machine and provision its on-chain identity
  - normalize raw OEM telemetry into canonical FCS data
  - query the machine's operational history
  - set up natural-language automations against canonical fields
  - anchor work on Solana mainnet via the MINT relay

Per-call gating (see gating.py): every tools/call must carry
`Authorization: Bearer fnet_…` on the wire. The middleware validates the
key against Supabase, looks up the tier (free → 4 read-only tools, 100
calls/mo; pro → all 11 tools, 10000 calls/mo), and atomically increments
a monthly counter. Tier or rate-limit violations surface as structured
JSON ToolError payloads with `upgrade_url: foundrynet.io/pricing`.

Upstream calls from each tool still use the shared FOUNDRYNET_API_KEY
configured on this service (Bearer fnet_… key created via /v1/keys on
the Forge service); per-user pass-through is a future deliverable.

Transport: SSE for remote Railway hosting. The SSE endpoint is at /sse;
clients (e.g. Claude Desktop via mcp-remote) connect there.

Health check: GET /health (returns config presence without leaking the key).

Required env:
  FOUNDRYNET_API_KEY    fnet_… key for the Forge user this server represents.

Optional env:
  FORGE_BASE_URL        Default https://forge.foundrynet.io
  PORT                  Default 8080 (Railway sets this automatically)
  REQUEST_TIMEOUT       HTTP timeout in seconds, default 30
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("foundrynet.mcp")

FORGE_BASE_URL     = (os.environ.get("FORGE_BASE_URL") or "https://forge.foundrynet.io").rstrip("/")
FOUNDRYNET_API_KEY = os.environ.get("FOUNDRYNET_API_KEY", "")
REQUEST_TIMEOUT    = int(os.environ.get("REQUEST_TIMEOUT", "30"))
PORT               = int(os.environ.get("PORT", "8080"))

if not FOUNDRYNET_API_KEY:
    logger.warning(
        "FOUNDRYNET_API_KEY is not set — every tool call will return a 401. "
        "Set it via the Railway dashboard before traffic hits this server."
    )

mcp = FastMCP("foundrynet")

# Per-client tier gating. Free keys see 7 tools (read-only + fire_sandbox +
# correct_mapping + get_coverage, 100/mo); Pro keys see all 14 (10 000/mo).
# Errors are ToolError-wrapped structured JSON payloads carrying
# upgrade_url/signup_url. See gating.py.
from gating import GatingMiddleware  # noqa: E402 — needs mcp instantiated first
mcp.add_middleware(GatingMiddleware())


# ── HTTP plumbing ────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {FOUNDRYNET_API_KEY}",
        "Content-Type":  "application/json",
        "User-Agent":    "FoundryNet-MCP/1.0",
    }


def _shape_error(status: int, body_text: str) -> dict:
    """Forge returns either a structured {detail: …} payload or HTML on
    edge errors. Normalize both into a single shape the LLM can read."""
    try:
        import json as _json
        return {"error": f"forge_{status}", "detail": _json.loads(body_text)}
    except Exception:
        return {"error": f"forge_{status}", "detail": body_text[:500]}


RETRY_DELAY_SECONDS = 2
RETRYABLE_EXCEPTIONS = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)


async def _call_forge(method: str, path: str, *,
                      body: Optional[dict] = None,
                      params: Optional[dict] = None) -> dict:
    """One HTTP call to Forge with one retry on transient transport errors.

    Always returns a dict (never raises) so the LLM gets a structured
    error in the tool result rather than an MCP-protocol exception. The
    -32602 transient failures observed in early Claude Desktop sessions
    came from upstream restarts during deploys; a single 2-second retry
    handles them without masking persistent issues.

    Retry policy: ConnectError / ReadTimeout / RemoteProtocolError → wait
    2s and try once more. Anything else (4xx/5xx HTTP, JSON decode
    failures, programmer errors) returns immediately with the error
    shape the LLM can read."""
    url = f"{FORGE_BASE_URL}{path}"
    last_exc: Optional[BaseException] = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                r = await client.request(method, url, headers=_headers(),
                                         json=body if body is not None else None,
                                         params=params)
        except RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt == 0:
                logger.info(f"_call_forge transient {type(e).__name__} on {method} {path}, retrying in {RETRY_DELAY_SECONDS}s")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            return {"error": "network",
                    "detail": f"{type(e).__name__}: {e}",
                    "attempts": 2}
        except httpx.HTTPError as e:
            # Non-retryable transport class — return immediately.
            return {"error": "network", "detail": f"{type(e).__name__}: {e}"}

        if r.status_code >= 400:
            return _shape_error(r.status_code, r.text)
        try:
            return r.json()
        except Exception as e:
            return {"error": "non_json_response",
                    "detail": f"{type(e).__name__}: {e}",
                    "raw":    r.text[:500]}

    # Unreachable, but type-checks: returned inside the loop on every path.
    return {"error": "unreachable", "detail": str(last_exc)}


# Back-compat alias — old name `_request` was used in earlier file revisions.
# Keep both bindings so anything in flight or in transit still works.
_request = _call_forge


# ── Tool 1: identify_machine ─────────────────────────────────────────────────

@mcp.tool
async def identify_machine(
    oem: str,
    model: str,
    serial: str,
    site: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Provision or retrieve a persistent on-chain identity (mint_id) for any
    industrial machine. Works for CNC machines, industrial robots, PLCs,
    additive manufacturing cells, injection molders, presses, turbines,
    pumps, compressors, conveyors — any equipment from any OEM:
    Fanuc, Siemens, Haas, DMG Mori, Mazak, Okuma, Hurco, Doosan, Makino,
    ABB, KUKA, Universal Robots, Yaskawa, Stäubli, FANUC Robotics,
    Komatsu, Caterpillar, John Deere, Trumpf, Bystronic, Amada, EMAG,
    Bosch Rexroth, Beckhoff, Rockwell Allen-Bradley.

    Returns the mint_id (universal handle, format "MINT-xxxxxx") plus its
    Solana wallet_address. Idempotent — calling again with the same
    (oem, model, serial) returns the same mint_id with `created: false`.

    USE WHEN: a user references a specific machine by OEM/model/serial and
    you need a stable handle to attach normalized data, automations, or
    on-chain settlements to. Always call this first when a new machine is
    introduced to the conversation, before normalize_telemetry or
    create_automation.
    """
    body: dict = {"oem": oem, "model": model, "serial": serial}
    if site is not None:     body["site"] = site
    if metadata is not None: body["metadata"] = metadata
    return await _call_forge("POST", "/v1/identify", body=body)


# ── Tool 2: normalize_telemetry ──────────────────────────────────────────────

@mcp.tool
async def normalize_telemetry(
    data: dict,
    machine_id: Optional[str] = None,
    oem: Optional[str] = None,
    model: Optional[str] = None,
    serial: Optional[str] = None,
    site: Optional[str] = None,
) -> dict:
    """Translate raw machine telemetry from any OEM's proprietary format
    into universal canonical FCS (FoundryNet Canonical Schema) data.
    Maps vendor-specific column names like "Spindle_Speed", "servo_load_x",
    "CoolantTemp", "FeedRateOverride" into standard fields like
    spindle_speed_rpm, axes.x_load_pct, sensor_readings.coolant_temp,
    feed_override_pct.

    Accepts a `data` dict of {raw_field: value}. If `machine_id` (mint_id
    or internal_id) is omitted but oem+model+serial are provided, silently
    auto-provisions the machine identity (same effect as calling
    identify_machine first).

    Each call:
      - Returns canonical_data + a per-field mapping_id (use mapping_id
        with /v1/feedback/{mapping_id}/correct if a mapping is wrong)
      - Writes a row to forge_normalized_history (visible via
        query_machine_history)
      - Evaluates active triggers; the response includes a `triggers_fired`
        array if any condition matched. The actual webhooks fire async, so
        the array tells you what was triggered without blocking on remote
        latency.

    USE WHEN: you have raw machine data — a CSV row, a sensor reading, an
    MES export, an alarm log line — and need to either (a) understand it
    semantically using canonical field names, (b) feed an automation that
    watches canonical fields, or (c) build up history for the machine.
    """
    body: dict = {"data": data}
    if machine_id is not None: body["machine_id"] = machine_id
    if oem is not None:        body["oem"] = oem
    if model is not None:      body["model"] = model
    if serial is not None:     body["serial"] = serial
    if site is not None:       body["site"] = site
    return await _call_forge("POST", "/v1/normalize", body=body)


# ── Tool 3: query_machine_history ────────────────────────────────────────────

@mcp.tool
async def query_machine_history(
    mint_id: str,
    from_dt: Optional[str] = None,
    to_dt: Optional[str] = None,
    fields: Optional[str] = None,
    limit: int = 100,
    summary: bool = False,
) -> dict:
    """Retrieve operational history for an identified machine. Each row is
    one /v1/normalize call's canonical output (FCS field → value).

    Query options:
      from_dt, to_dt   ISO-8601 timestamps to bound the time range
      fields           comma-separated FCS field names to project; omit for
                       full canonical_data
      limit            max rows (1–1000, default 100)
      summary          true → returns aggregate stats only (row_count,
                       time range, avg coverage_pct, fields_covered set)
                       without the raw rows. Always cheap.

    USE WHEN: a user asks how a machine has been running, wants utilization
    or throughput or health trends, looks for patterns in alarms or
    operational state, compares periods ("how was today vs yesterday"), or
    wants to know what data is even available for a machine. Prefer
    `summary=true` first to orient on volume + which fields are present,
    then drill in with field projection on a smaller time window.
    """
    params: dict = {"limit": limit, "summary": str(summary).lower()}
    if from_dt is not None: params["from"] = from_dt
    if to_dt is not None:   params["to"]   = to_dt
    if fields is not None:  params["fields"] = fields
    return await _call_forge("GET", f"/v1/history/{mint_id}", params=params)


# ── Tool 4: create_automation ────────────────────────────────────────────────

@mcp.tool
async def create_automation(
    mint_id: str,
    instruction: str,
) -> dict:
    """Set up automated monitoring + actions for an industrial machine
    using natural language. Connect machine telemetry to any business
    system — ERP, CMMS, MES, Slack, Teams, email, Zapier, n8n — via
    webhooks already registered as tools on the Forge service.

    Examples of `instruction`:
      "Alert maintenance Slack when spindle load exceeds 90 percent."
      "Create a Fiix work order when coolant temperature stays above 35°C
       for five minutes."
      "Notify the supervisor when part_count hits 500."
      "When the maintenance_type changes to CORRECTIVE, post to the ops
       channel."

    Returns a `parsed_trigger` JSON for HUMAN review — DOES NOT
    auto-activate. The caller (you, with user confirmation) must
    explicitly POST the parsed_trigger to /v1/triggers on the Forge API
    to actually create it. The response includes
    `confirmation_required: true` and may include `notes` if the parser
    had to make a fuzzy match (e.g. resolved an ambiguous field name to
    its closest canonical match).

    USE WHEN: a user wants to set up monitoring, alerts, or automations
    for machine state transitions. Always show the parsed_trigger to the
    user verbatim and ask "Confirm to activate?" before they activate it.
    """
    # The MCP tool exposes `mint_id` as the parameter name (consistent with
    # the rest of the v1 surface), but /v1/triggers/natural's body schema
    # uses `machine_id` (which accepts mint_id OR internal_id) — map here.
    return await _call_forge(
        "POST", "/v1/triggers/natural",
        body={"machine_id": mint_id, "instruction": instruction},
    )


# ── Tool 5: activate_automation ──────────────────────────────────────────────

@mcp.tool
async def activate_automation(
    machine_id: str,
    name: str,
    condition: dict,
    actions: list,
    enabled: bool = True,
) -> dict:
    """Activate a parsed automation trigger on a machine. Call this AFTER
    create_automation returns a parsed_trigger and the user explicitly
    confirms they want to arm it.

    Creates a live trigger that monitors the machine's normalized telemetry
    and fires the listed actions when the condition matches. Each action
    references a registered tool by tool_id; on fire, the tool's webhook
    is POSTed with {{variable}} interpolation against the canonical data
    context (mint_id, oem, model, serial, site, field, value, threshold,
    plus every canonical field on the matched record).

    Inputs:
      machine_id  mint_id ("MINT-…") or internal_id; resolved to canonical mint_id
      name        short human label, ≤ 80 chars (e.g. "high spindle load")
      condition   simple {field, op, value|threshold} OR compound {all: [...]}
                  ops: >, <, >=, <=, ==, !=
      actions     list of {tool_id, payload_overrides?, headers_overrides?}
      enabled     defaults to true; pass false to create the trigger paused

    Returns the persisted trigger row including `id` (use it later to
    pause/edit/delete via the Forge API). Once active, the trigger fires
    on every subsequent normalize_telemetry call where the condition
    matches — no further activation needed.

    USE WHEN: the user has reviewed the parsed_trigger from
    create_automation and said something like "yes, activate it" /
    "go ahead" / "arm it." Never call this tool without explicit
    confirmation — it changes machine behavior in a way the user can
    feel (real Slack messages, real ERP work orders).
    """
    return await _call_forge(
        "POST", "/v1/triggers",
        body={
            "machine_id": machine_id,
            "name":       name,
            "condition":  condition,
            "actions":    actions,
            "enabled":    enabled,
        },
    )


# ── Tool 6: list_automations ─────────────────────────────────────────────────

@mcp.tool
async def list_automations(machine_id: str) -> dict:
    """List all active automations / triggers configured for one machine.

    Returns each trigger with: id, name, condition (field/op/value or
    compound `all`), actions (each resolved to its tool name + url +
    method), enabled state, fire_count, last_fired_at, last_error.

    USE WHEN: the user asks "what automations do I have on this machine"
    / "show me my triggers" / "what alerts am I getting" / "what's
    monitoring this machine right now". Always pass the machine's
    mint_id (or internal_id — both resolve)."""
    return await _call_forge("GET", "/v1/triggers", params={"machine_id": machine_id})


# ── Tool 7: disable_automation ───────────────────────────────────────────────

@mcp.tool
async def disable_automation(trigger_id: str) -> dict:
    """Pause an automation trigger without deleting it. The trigger stops
    evaluating against incoming /v1/normalize calls but its configuration
    (condition, actions, history) is preserved. Re-enable later by
    PATCHing /v1/triggers/{id} with `{"enabled": true}` (or by asking
    the user to confirm and creating a follow-up tool for resume).

    USE WHEN: the user wants to TEMPORARILY stop an automation — e.g.
    "pause the high-spindle alert during planned maintenance," "stop
    that alarm for now, I'll re-enable it tomorrow." Distinct from
    delete_automation, which is permanent."""
    return await _call_forge("PATCH", f"/v1/triggers/{trigger_id}",
                             body={"enabled": False})


# ── Tool 8: delete_automation ────────────────────────────────────────────────

@mcp.tool
async def delete_automation(trigger_id: str) -> dict:
    """Soft-deletes the trigger (recoverable for 30 days via
    restore_automation). The trigger immediately stops evaluating against
    /v1/normalize calls and is hidden from list_automations, but the row
    persists with deleted_at set so an accidental delete can be undone.
    Use restore_automation to undo. For permanent deletion, the API
    supports ?permanent=true.

    Past forge_trigger_executions rows for this trigger remain in either
    case (audit trail).

    USE WHEN: the user wants to remove an automation they no longer need —
    "delete the coolant alert," "remove that trigger." Safer than hard
    delete because misclicks are recoverable; tell the user about
    restore_automation if they later change their mind."""
    return await _call_forge("DELETE", f"/v1/triggers/{trigger_id}")


# ── Tool 9: query_webhook_history ────────────────────────────────────────────

@mcp.tool
async def query_webhook_history(trigger_id: str, limit: int = 10) -> dict:
    """Show webhook delivery history for a trigger — HTTP status codes,
    response times, retry counts, errors. Use to verify webhooks are
    actually delivering.

    Returns up to `limit` most-recent execution rows (default 10, max 200),
    each with: fired_at, http_status, attempt_count, response_time_ms,
    error (if any), tool_name, target_url, and the on-chain settlement
    tx (settled_tx) once the row has been Merkle-rooted via batch settle.

    USE WHEN: a user asks "did the alert actually go out?" / "why didn't
    Slack get pinged?" / "is the trigger working?" / "show me the last
    few fires." Soft-deleted triggers can still be queried — useful for
    forensic audits after a misclick + restore."""
    return await _call_forge("GET", f"/v1/triggers/{trigger_id}/executions",
                             params={"limit": limit})


# ── Tool 10: restore_automation ──────────────────────────────────────────────

@mcp.tool
async def restore_automation(trigger_id: str) -> dict:
    """Restore a previously soft-deleted automation trigger within its 30-day
    recovery window. Re-enables the trigger so it evaluates against
    incoming /v1/normalize calls again.

    Returns the restored trigger row plus `restored: true` and the
    `restored_at` timestamp. 410 (Gone) if the trigger was deleted more
    than 30 days ago and is past the restorable window. 409 if the
    trigger isn't actually deleted.

    USE WHEN: a user accidentally deleted a trigger and wants it back.
    Also useful as the "undo" half of a "delete then change my mind"
    flow — pair with disable_automation when the user wants to pause
    rather than delete in the first place."""
    return await _call_forge("PATCH", f"/v1/triggers/{trigger_id}/restore")


# ── Tool 11: verify_on_chain ─────────────────────────────────────────────────

@mcp.tool
async def verify_on_chain(
    mint_id: Optional[str] = None,
    payload: Optional[dict] = None,
    batch: bool = False,
) -> dict:
    """Anchor data on Solana mainnet via the MINT relay for cryptographic
    proof of work. Two modes:

    BATCH MODE (`batch=true`, requires `mint_id`):
      Collects every unsettled event for that machine — normalize calls,
      trigger fires, webhook executions — since the last batch. Computes
      a Merkle root of their event hashes and anchors that single root on
      Solana. ONE transaction proves dozens to thousands of events.
      Returns: merkle_root, event_count, event_types breakdown,
      tx_signature, verify_url (Solscan link). Cost-efficient — call this
      once an hour or once a shift per machine, not per event.

    SINGLE-PAYLOAD MODE (`batch=false`, requires `payload`):
      Hashes an arbitrary JSON `payload` deterministically (sorted keys,
      no whitespace) and anchors the hash. Returns: payload_hash,
      tx_signature, verify_url. Use for one-off proofs — inspection
      records, completed work orders, signed reports — where you want a
      permanent independent timestamp.

    USE WHEN: a user wants tamper-proof evidence — settlement of a
    completed work batch, proof a maintenance window happened, anchoring
    a quality report, rolling up a day's machine activity into a single
    verifiable hash. ALWAYS include the `verify_url` (a Solscan link) in
    your reply so the user can independently verify on-chain.
    """
    if batch:
        if not mint_id:
            return {"error": "bad_request",
                    "detail": "batch=true requires mint_id"}
        return await _call_forge("POST", "/v1/settle",
                              body={"mint_id": mint_id},
                              params={"batch": "true"})
    if payload is None:
        return {"error": "bad_request",
                "detail": "batch=false requires `payload` (any JSON object)"}
    return await _call_forge("POST", "/v1/settle", body=payload)


# ── Tool 12: fire_sandbox — FREE-tier demo of the full action loop ────────
# Gating: FREE in gating.FREE_TOOLS, capped at 10 lifetime fires per fnet_
# key via gating.SANDBOX_FIRE_CAP. Demonstrates the watch→fire→settle loop
# end-to-end without any real machine onboarding or paid tier — the
# developer types a condition + a target message, the MCP server POSTs to
# its own /sandbox/echo endpoint (so there's a real HTTP round-trip with a
# real response body), hashes the result, and anchors that hash on Solana
# via the MINT relay. Returns the echo response, the tx_signature, and a
# Solscan verify_url so the dev can see the on-chain receipt themselves.

@mcp.tool
async def fire_sandbox(
    condition_text: str,
    message: str = "Spindle load crossed 85%. Sandbox demo fire.",
) -> dict:
    """Demo the full Forge watch→fire→settle loop against a built-in sandbox
    endpoint. Free tier; no machine onboarding required.

    The MCP server POSTs `{message, condition: condition_text, ts}` to its
    own /sandbox/echo route — a real HTTP round-trip with a real response
    body — then hashes the response and anchors it on Solana mainnet via
    the MINT relay. Returns the echo body, the tx_signature, and a Solscan
    verify_url.

    USE WHEN: a developer is evaluating Forge and wants to feel the full
    loop (a webhook actually fires, a real Solana tx actually settles, the
    Solscan link actually verifies) without onboarding any machines or
    paying for the Pro tier. 10 fires lifetime per fnet_ key.

    Args:
        condition_text: A plain-English description of the condition the
            sandbox is simulating, e.g. "Spindle load crossed 85%".
        message: The payload text the sandbox webhook receives. Defaults
            to a representative example.
    """
    import hashlib, json as _json
    import datetime as _dt
    sandbox_url = "https://foundrynet-mcp-production.up.railway.app/sandbox/echo"
    payload = {
        "condition": condition_text,
        "message":   message,
        "ts":        _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    # Build the payload deterministically so the hash is reproducible.
    payload_json = _json.dumps(payload, sort_keys=True, separators=(",", ":"))

    # 1) Fire the sandbox webhook — real HTTP, real response.
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            wr = await client.post(sandbox_url, content=payload_json,
                                   headers={"Content-Type": "application/json"})
        echo_status = wr.status_code
        echo_body = wr.text
    except Exception as e:
        return {"error": "sandbox_webhook_failed",
                "detail": f"{type(e).__name__}: {e}"}

    # 2) Hash the action+inputs+outputs deterministically.
    canonical = _json.dumps(
        {"action": "sandbox_fire", "inputs": payload,
         "outputs": {"status": echo_status, "body": echo_body[:500]}},
        sort_keys=True, separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # 3) Settle on Solana via /v1/settle (single-payload mode).
    settle = await _call_forge("POST", "/v1/settle",
                               body={"payload_hash": payload_hash,
                                     "action": "sandbox_fire"})
    return {
        "sandbox_echo": {"status": echo_status, "body_preview": echo_body[:240]},
        "payload_hash": payload_hash,
        "tx_signature": settle.get("tx_signature"),
        "verify_url":   settle.get("verify_url"),
        "note": (
            "Demo loop verified — Solscan link is a real on-chain anchor. "
            "Lifetime cap 10/key. Upgrade for unlimited + real-machine "
            "automations at https://foundrynet.io/pricing"
        ),
    }


# ── Tool 13: correct_mapping ─────────────────────────────────────────────────

def _feedback_mapping_id(source_field: str, oem_hint: Optional[str]) -> str:
    """Replicate forge-prod `_mapping_id` so a correction can be filed even when
    the caller didn't retain the mapping_id from normalize_telemetry. Must stay
    byte-identical to api.py:_mapping_id (sha256 of 'field|oem', lowered)."""
    import hashlib
    key = f"{(source_field or '').strip().lower()}|{(oem_hint or '').strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@mcp.tool
async def correct_mapping(
    source_field: str,
    confirmed_canonical: str,
    original_canonical: str,
    oem: Optional[str] = None,
    mapping_id: Optional[str] = None,
    sample_value: Optional[str] = None,
    confidence: Optional[float] = None,
) -> dict:
    """Teach Forge the RIGHT canonical field for a source column that
    normalize_telemetry mapped wrong (or abstained on). Each correction is
    recorded as a corpus-improvement signal the retrainer uses to fix the
    mapping for everyone — so every agent interaction makes normalization better.

    USE WHEN: you or the user can see normalize_telemetry returned the wrong
    canonical for a field (e.g. it mapped an oil-pressure column to a tire-
    pressure field), or it abstained on a field whose meaning you know.

    - source_field: the raw column name exactly as it appeared in your data.
    - confirmed_canonical: the canonical field it SHOULD map to.
    - original_canonical: what normalize_telemetry actually returned (pass the
      `canonical` from that field's entry; use "abstained" if it abstained).
    - oem: the OEM you passed to normalize_telemetry (improves aggregation).
    - mapping_id: optional — the `mapping_id` from the normalize_telemetry field
      entry. If omitted it is derived deterministically from (source_field, oem).

    Returns {ok, feedback_id, action:"correct"}. Corrections feed an offline
    retrain (they don't hot-patch the live corpus), so noisy feedback can't
    poison other users' mappings.
    """
    mid = mapping_id or _feedback_mapping_id(source_field, oem)
    body: dict = {
        "source_field":        source_field,
        "original_canonical":  original_canonical or "abstained",
        "confirmed_canonical": confirmed_canonical,
    }
    if oem is not None:          body["oem_hint"] = oem
    if sample_value is not None: body["sample_value"] = sample_value
    if confidence is not None:   body["confidence"] = confidence
    return await _call_forge("POST", f"/v1/feedback/{mid}/correct", body=body)


# ── Tool 14: get_coverage ────────────────────────────────────────────────────

@mcp.tool
async def get_coverage(oem: Optional[str] = None) -> dict:
    """Ask Forge what it can normalize BEFORE you try: the recognized OEM
    verticals (CNC / robot / vehicle / AMR), the canonical-field families, and
    the field list per family. Optionally pass an `oem` to see which vertical it
    resolves to and whether the cross-vertical gate will engage.

    USE WHEN: starting a new integration, or deciding whether to call
    normalize_telemetry — confirm the machine's OEM and your fields are in
    coverage. Unknown OEMs still normalize (the gate just disables itself), so
    absence here is a soft signal, not a hard block.
    """
    params = {"oem": oem} if oem else None
    return await _call_forge("GET", "/v1/coverage", params=params)


# ── Health route (mounted on the SSE app via FastMCP custom_route) ──────────

@mcp.custom_route("/sandbox/echo", methods=["POST"])
async def sandbox_echo(request: Request) -> JSONResponse:
    """Sandbox webhook target for fire_sandbox tool. Returns the received
    JSON body back with a 200 + an `echoed_at` timestamp. Open / unauthed —
    only the fire_sandbox tool calls it during normal flow, but no creds are
    exposed even if it's hit directly."""
    import datetime as _dt
    try:
        body = await request.json()
    except Exception:
        body = {"raw": (await request.body()).decode("utf-8", errors="replace")[:500]}
    return JSONResponse({
        "ok":         True,
        "echoed_at":  _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "received":   body,
    })


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Health check for Railway + load balancers. Reports config presence
    without ever leaking the API key value."""
    return JSONResponse({
        "status":             "ok",
        "service":            "foundrynet-mcp",
        "forge_base_url":     FORGE_BASE_URL,
        "key_configured":     bool(FOUNDRYNET_API_KEY),
        "transport":          "sse",
        "gating":             "per_client_fnet_2tier",
        "supabase_configured": bool(os.environ.get("SUPABASE_URL")
                                    and os.environ.get("SUPABASE_SERVICE_KEY")),
        "pricing_url":        os.environ.get("MCP_PRICING_URL", "https://foundrynet.io/pricing"),
    })


# ── /.well-known/mcp* — public discovery endpoints ──────────────────────────
# Auth-free metadata that MCP crawlers / hubs (glama.ai, smithery, mcp.so,
# pulsemcp, awesome-mcp-servers) pull to enumerate this server without a
# registry round-trip. Payload schema follows the de-facto well-known/mcp
# layout used across those directories. Schema rebased 2026-05-28 to the
# canonical spec — adds schema_version, tagline, auth{}, pricing{}, docs_url,
# logo_url; renames `url`+`transport`{} → `server_url`+`transport`. Cache
# allowed at the edge (5 min) since payload only changes on a redeploy.
# Static payload — never reads env or DB, so it can't leak secrets.

@mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
async def server_card(request: Request) -> JSONResponse:
    """Full MCP server card — the per-server metadata directories consume.

    Schema rebased 2026-05-28 to camelCase per the emerging well-known/mcp
    convention: `version` (was `schema_version`), `serverUrl` (was
    `server_url`). Added full `tools[]` array — directories like glama,
    smithery, and pulsemcp surface per-tool descriptions on the listing
    page when this is present. One-liners only; the verbose docstrings live
    in each tool's `@mcp.tool` decorator and are returned via tools/list."""
    return JSONResponse(
        {
            "version":   "1.0",
            "name":      "FoundryNet Forge",
            "tagline":   "Plain English in. Autonomous action out.",
            "description": (
                "Watch any signal across 16 OEM families — Fanuc, Siemens, "
                "Haas, DMG Mori, Mazak, Okuma, Doosan, Makino, ABB, KUKA, "
                "Universal Robots, Yaskawa, Stäubli, Trumpf, Bystronic, "
                "Bosch Rexroth — backed by 18,785+ canonical field mappings. "
                "When a condition matches, Forge fires the webhook or alert "
                "you wired (Slack, Teams, PagerDuty, your MES, your own "
                "endpoint) and anchors a tamper-evident hash of the action "
                "on Solana via the MINT relay. The agent watches; you stay "
                "in the loop."
            ),
            "serverUrl": "https://foundrynet-mcp-production.up.railway.app/sse",
            "transport": "sse",
            "auth": {
                "type":       "api_key",
                "header":     "Authorization",
                "prefix":     "Bearer",
                "signup_url": "https://foundrynet.io/signup",
            },
            # Ready-to-paste install snippets so directory crawlers and
            # first-run users get the auth header correctly. Without the
            # `--header Authorization:Bearer …` line, mcp-remote opens the
            # SSE stream unauthed and every tool call comes back missing_api_key.
            "installations": {
                "claude_desktop": (
                    '{\n'
                    '  "mcpServers": {\n'
                    '    "foundrynet-forge": {\n'
                    '      "command": "npx",\n'
                    '      "args": ["-y", "mcp-remote",\n'
                    '               "https://foundrynet-mcp-production.up.railway.app/sse",\n'
                    '               "--header", "Authorization:Bearer ${FNET_KEY}"],\n'
                    '      "env": { "FNET_KEY": "fnet_…  (free key at foundrynet.io/signup)" }\n'
                    '    }\n'
                    '  }\n'
                    '}'
                ),
                "cursor": (
                    '{\n'
                    '  "mcpServers": {\n'
                    '    "foundrynet-forge": {\n'
                    '      "command": "npx",\n'
                    '      "args": ["-y", "mcp-remote",\n'
                    '               "https://foundrynet-mcp-production.up.railway.app/sse",\n'
                    '               "--header", "Authorization:Bearer ${FNET_KEY}"],\n'
                    '      "env": { "FNET_KEY": "fnet_…" }\n'
                    '    }\n'
                    '  }\n'
                    '}'
                ),
                "claude_code": (
                    "claude mcp add foundrynet-forge -- "
                    "npx -y mcp-remote "
                    "https://foundrynet-mcp-production.up.railway.app/sse "
                    "--header \"Authorization:Bearer ${FNET_KEY}\""
                ),
            },
            "tools_count": 14,
            "tools": [
                {"name": "identify_machine",
                 "description": "Provision a stable mint_id and Solana wallet address for an OEM/model/serial machine. Idempotent."},
                {"name": "normalize_telemetry",
                 "description": "Translate raw OEM telemetry into canonical FCS fields across 16 OEM families and 18,000+ field mappings."},
                {"name": "query_machine_history",
                 "description": "Read normalized operational history for a machine with field projection, time-range filters, and summary mode."},
                {"name": "create_automation",
                 "description": "Parse a plain-English instruction into a structured trigger — preview only, requires explicit activation."},
                {"name": "activate_automation",
                 "description": "Activate a parsed trigger so it fires its registered actions when the condition matches canonical telemetry."},
                {"name": "list_automations",
                 "description": "List the active trigger automations configured for a machine, with their conditions and actions."},
                {"name": "disable_automation",
                 "description": "Pause an active trigger without deleting it; configuration and history are preserved."},
                {"name": "delete_automation",
                 "description": "Soft-delete a trigger with a 30-day restore window; past execution rows remain for audit."},
                {"name": "restore_automation",
                 "description": "Restore a soft-deleted trigger inside its 30-day recovery window."},
                {"name": "query_webhook_history",
                 "description": "Webhook delivery history for a trigger — HTTP status, retries, response times, on-chain settlement signatures."},
                {"name": "verify_on_chain",
                 "description": "Anchor work on Solana mainnet via the MINT relay; returns merkle_root / payload_hash, tx_signature, and a Solscan verify_url."},
                {"name": "fire_sandbox",
                 "description": "FREE-tier demo: fires a sample condition at the built-in /sandbox/echo endpoint, captures the response, and settles a hash on Solana mainnet. Demonstrates the full watch→fire→settle loop end-to-end. Lifetime cap: 10 fires per fnet_ key — no credit card required."},
                {"name": "correct_mapping",
                 "description": "FREE: teach Forge the right canonical field when normalize_telemetry mapped a column wrong or abstained. Each correction is a corpus-improvement signal feeding an offline retrain."},
                {"name": "get_coverage",
                 "description": "FREE: schema introspection — recognized OEM verticals (CNC/robot/vehicle/AMR) and canonical-field families, so an agent can check coverage before calling normalize_telemetry."},
            ],
            "categories": [
                "industrial", "manufacturing", "iot",
                "automation", "blockchain",
            ],
            # Pay-per-use metered billing (Stripe Meter Events — wired in
            # billing.py). No flat monthly tier; the bill is whatever you
            # actually consume. Calculator examples below are for a single
            # machine at the named sample cadence; usage scales linearly with
            # machine count and inversely with sample interval.
            "pricing": {
                "model":     "metered",
                "free_tier": "100 tool calls/month, read-only tools — no card",
                "paid_from": "Pay-per-use — see examples",
                "examples": [
                    {"scenario": "1 CNC sampled every 30 seconds", "estimate_usd_per_month": 13},
                    {"scenario": "1 CNC sampled every 10 seconds", "estimate_usd_per_month": 40},
                    {"scenario": "1 CNC sampled every 1 second",   "estimate_usd_per_month": 260},
                ],
                "pricing_url": "https://foundrynet.io/pricing",
            },
            "docs_url": "https://foundrynet.io/docs",
            "logo_url": "https://foundrynet.io/logo.png",
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@mcp.custom_route("/.well-known/mcp", methods=["GET"])
async def mcp_endpoints(request: Request) -> JSONResponse:
    """Discovery list — the entry point a crawler dereferences first to
    enumerate which MCP servers this origin exposes (we expose one)."""
    return JSONResponse(
        {
            "endpoints": [{
                "url":       "https://foundrynet-mcp-production.up.railway.app/sse",
                "transport": "sse",
                "name":      "FoundryNet Forge MCP",
            }],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(
        f"FoundryNet MCP starting on 0.0.0.0:{PORT} "
        f"(forge={FORGE_BASE_URL}, key_configured={bool(FOUNDRYNET_API_KEY)})"
    )
    mcp.run(transport="sse", host="0.0.0.0", port=PORT)
