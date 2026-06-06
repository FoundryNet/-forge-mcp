# FoundryNet — Industrial Machine Intelligence

The cross-manufacturer industrial MCP server. Talk to any CNC, robot, or industrial
machine in natural language — machine identity, telemetry normalization across 16 OEM
families, plain-English automation, and tamper-evident on-chain work attestation.

Hosted MCP over SSE. 14 tools wrap the Forge v1 API: provision a stable machine identity,
normalize raw OEM telemetry into a canonical schema, query operational history, parse and
activate plain-English automations, and settle work on Solana via the MINT relay so every
state-changing action has a verifiable, tamper-evident hash.

- **Website:** https://foundrynet.io
- **Docs:** https://foundrynet.io/docs · **Free key:** https://foundrynet.io/signup
- **SSE endpoint:** `https://foundrynet-mcp-production.up.railway.app/sse`
- **Health:** `https://foundrynet-mcp-production.up.railway.app/health`
- **Server card:** `https://foundrynet-mcp-production.up.railway.app/.well-known/mcp/server-card.json`

## What it does

It normalizes raw OEM telemetry from **16 manufacturer families** into one canonical
vocabulary with thousands of confirmed field mappings — so an agent writes against one set
of field names whether the machine is a Fanuc CNC, a KUKA arm, or a Universal Robots cobot.
On top of that it turns plain-English instructions into structured automations (review then
activate), and anchors every state-changing action on-chain via MINT for a tamper-evident
work record.

## Architecture

Pure HTTP proxy. Every tool is a thin wrapper around `https://forge.foundrynet.io/v1/*`
using a configured `fnet_` Bearer key. No state, no shared imports with `forge-prod` —
separate Railway service, separate dependencies (`fastmcp` + `httpx`).

```
Claude Desktop / agent
        │ SSE (mcp-remote bridge)
        ▼
  foundrynet-mcp on Railway
        │ HTTPS + Bearer fnet_…
        ▼
  forge.foundrynet.io/v1/*
```

## Tools (14)

Identity & data: `identify_machine`, `normalize_telemetry`, `query_machine_history`,
`get_coverage`, `correct_mapping`. Automation: `create_automation`, `activate_automation`,
`list_automations`, `disable_automation`, `delete_automation`, `restore_automation`,
`query_webhook_history`. Attestation: `verify_on_chain`. Demo: `fire_sandbox` (the full
watch → fire → settle loop, no card).

Free tier exposes the read-only tools; Pro ($49/mo) unlocks the full set.

## Connect (Claude Desktop, Cursor, any MCP client)

```bash
claude mcp add --transport sse foundrynet \
  https://foundrynet-mcp-production.up.railway.app/sse \
  --header "Authorization: Bearer fnet_YOUR_KEY"
```

Or via `claude_desktop_config.json` with the `mcp-remote` bridge:

```json
{
  "mcpServers": {
    "foundrynet": {
      "command": "npx",
      "args": ["-y", "mcp-remote",
               "https://foundrynet-mcp-production.up.railway.app/sse",
               "--header", "Authorization:Bearer ${FNET_KEY}"],
      "env": { "FNET_KEY": "fnet_…  (get a free key at foundrynet.io/signup)" }
    }
  }
}
```

Get a free `fnet_` key at https://foundrynet.io/signup (50 normalize calls, no card).

## Required environment (server-side)

| Var | Required | Default |
|---|---|---|
| `FOUNDRYNET_API_KEY` | Yes | — (server boots; tool calls return 401 until set) |
| `FORGE_BASE_URL` | No | `https://forge.foundrynet.io` |
| `PORT` | No | 8080 (Railway sets this automatically) |
| `REQUEST_TIMEOUT` | No | 30 (seconds) |

## Files

- `mcp_server.py` — the server (14 tools + `/health` + `/.well-known/mcp` routes)
- `gating.py` — per-client tier gating (Free vs Pro tool/quota enforcement)
- `server.json` — MCP registry metadata (name, description, keywords, remote endpoint)
- `smithery.yaml` — Smithery listing metadata
- `requirements.txt` — `fastmcp>=2.0`, `httpx>=0.27`
- `Procfile` — Railway start command (`web: python mcp_server.py`)

## License

Proprietary (commercial). © FoundryNet. Contact: foundrynet@proton.me
