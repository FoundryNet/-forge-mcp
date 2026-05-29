# FoundryNet Forge MCP

> **Plain English in. Autonomous action out.** Watch any signal across 16 OEM families. When a condition matches, Forge fires the webhook or alert you wired and anchors a tamper-evident hash of the action on Solana.

[![MINT Attested](https://img.shields.io/badge/MINT-Attested-d4af37?style=for-the-badge&labelColor=08080d)](https://physicalmcp.vercel.app/#mint)
[![Listed on PhysicalMCP](https://img.shields.io/badge/Listed%20on-PhysicalMCP-2dbb78?style=for-the-badge&labelColor=08080d)](https://physicalmcp.vercel.app/)
[![Schema 1.0](https://img.shields.io/badge/MCP-1.0-blue?style=for-the-badge&labelColor=08080d)](https://modelcontextprotocol.io/)

12 Model Context Protocol tools wrapping the [Forge](https://foundrynet.io) v1 API. Cross-manufacturer telemetry normalization across **16 OEM families** with **18,000+ field mappings**: Fanuc, Siemens, Haas, DMG Mori, Mazak, Okuma, Doosan, Makino, ABB, KUKA, Universal Robots, Yaskawa, Stäubli, Trumpf, Bystronic, Bosch Rexroth. When a watched condition matches, Forge fires the webhook or alert you wired (Slack, Teams, PagerDuty, your MES, your own endpoint) and anchors a tamper-evident hash of the action on Solana mainnet via the [MINT Protocol](https://foundrynet.io) relay — any third party can verify *the action happened with these inputs against this machine* via a Solscan link, no trust in the server required.

**Forge fires webhooks and creates alerts when conditions match. It doesn't push setpoints to PLCs directly** — the agent watches, you stay in the loop, and your existing automation stack does what it already does best.

> **This repo is the public discovery marker for the hosted server.** The MCP server itself runs on Railway at `https://foundrynet-mcp-production.up.railway.app/sse`. There is no source code in this repository by design (the implementation is commercial). The README + the live `/.well-known/mcp/server-card.json` are the canonical metadata sources for directories and crawlers.

## At a glance

| | |
|---|---|
| **Transport** | SSE (Streamable-HTTP-compatible via [`mcp-remote`](https://github.com/geelen/mcp-remote)) |
| **Auth** | API key — `Authorization: Bearer fnet_…` |
| **Tools** | 12 (see below) |
| **License** | Commercial. Free tier available. |
| **Free tier** | 100 tool calls/month, 5 tools including `fire_sandbox` for the full-loop demo (10 sandbox fires lifetime). No card. |
| **Paid** | Pay-per-use metered billing — see *Pricing* below |
| **Sign up** | https://foundrynet.io/signup |
| **Docs** | https://foundrynet.io/docs |
| **Pricing** | https://foundrynet.io/pricing |
| **Server card** | https://foundrynet-mcp-production.up.railway.app/.well-known/mcp/server-card.json |

## Pricing — pay for what you actually use

Forge bills per call, not per machine and not per month. The bill is whatever you actually consume.

A quick calculator for typical sample cadences against one machine:

| Sample interval | Tool calls / mo | Estimated cost |
|---|---:|---:|
| Every 30 seconds | ~86 400 | **~$13 / month** |
| Every 10 seconds | ~259 200 | **~$40 / month** |
| Every 1 second | ~2 592 000 | **~$260 / month** |

Scales linearly with machine count; halves when you halve the sample rate. Heavy-action workloads (alerts firing constantly) cost a little more because of meter-events on `trigger_fire` and `settle_batch_call`; pure-read workloads are cheaper. There is no flat $/machine/month tier. See [foundrynet.io/pricing](https://foundrynet.io/pricing) for the live unit prices and to bring your own card.

## What it actually does

Three layers of capability, all behind one MCP server:

1. **Identity & normalization.** `identify_machine` provisions a stable `mint_id` across OEM serials. `normalize_telemetry` translates raw OEM column names like `Spindle_Speed`, `servo_load_x`, `CoolantTemp` into a canonical schema (`spindle_speed_rpm`, `axes.x_load_pct`, `sensor_readings.coolant_temp`) using 18,000+ field mappings learned from real industrial datasets.
2. **History & automation.** `query_machine_history` returns canonical operational data; `create_automation` parses plain-English instructions ("alert maintenance Slack when spindle load exceeds 90 percent") into structured triggers; `activate_automation` arms them. `list_automations`, `disable_automation`, `delete_automation`, `restore_automation`, and `query_webhook_history` round out the lifecycle.
3. **On-chain settlement.** `verify_on_chain` anchors action hashes on Solana mainnet via the MINT relay. Single-payload mode for one-off proofs, batch mode for hourly Merkle-rooted rollups. Every response includes a Solscan `verify_url`.

## The 12 tools

| Tool | What it does | Tier |
|---|---|---|
| `fire_sandbox` | Demo the full watch→fire→settle loop against a built-in echo endpoint; real Solana tx, real Solscan link | **Free demo** · 10 fires lifetime |
| `normalize_telemetry` | Translate raw OEM telemetry into canonical FCS fields | Free |
| `query_machine_history` | Read normalized operational history with field projection | Free |
| `list_automations` | List active triggers for a machine | Free |
| `query_webhook_history` | Trigger-fire delivery log + on-chain settlement signatures | Free |
| `identify_machine` | Provision a stable `mint_id` for an OEM/model/serial | Pro |
| `create_automation` | Parse plain-English instruction → structured trigger (preview) | Pro |
| `activate_automation` | Arm a parsed trigger to fire actions | Pro |
| `disable_automation` | Pause a trigger without deletion | Pro |
| `delete_automation` | Soft-delete with 30-day restore window | Pro |
| `restore_automation` | Undo a soft-delete inside the restore window | Pro |
| `verify_on_chain` | Anchor work on Solana mainnet via the MINT relay | Pro |

## Install — Claude Desktop

Get a free `fnet_` key at https://foundrynet.io/signup, then add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "foundrynet-forge": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://foundrynet-mcp-production.up.railway.app/sse",
        "--header",
        "Authorization:Bearer ${FNET_KEY}"
      ],
      "env": {
        "FNET_KEY": "fnet_…replace_with_your_key…"
      }
    }
  }
}
```

Config locations:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Restart Claude Desktop; the 11 tools appear in the slash-command palette.

## Install — Cursor

Add to `~/.cursor/mcp.json` (same shape as Claude Desktop):

```json
{
  "mcpServers": {
    "foundrynet-forge": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://foundrynet-mcp-production.up.railway.app/sse",
        "--header",
        "Authorization:Bearer ${FNET_KEY}"
      ],
      "env": {
        "FNET_KEY": "fnet_…replace_with_your_key…"
      }
    }
  }
}
```

Cursor → Settings → MCP → reload. Tools become available across all chats.

## Install — Claude Code

```bash
claude mcp add foundrynet-forge -- \
  npx -y mcp-remote \
  https://foundrynet-mcp-production.up.railway.app/sse \
  --header "Authorization:Bearer fnet_…replace…"
```

## Smoke test

Once installed, ask your assistant something like:

> *"Identify a Fanuc R-30iB with serial VERIFY-001 and tell me its mint_id and Solana wallet address."*

If `identify_machine` is called and returns a `MINT-…` id, you're wired up.

## Verifying a MINT-attested action

Every state-changing tool response includes a `verify_url` pointing at Solscan. Paste it into a browser and you'll see the on-chain transaction hashing the action's inputs, outputs, and machine identity — independently verifiable, no trust in this server required.

## Discovery endpoints

Public, auth-free, served by the same MCP origin:

- `GET /.well-known/mcp` — `{"endpoints": [{...}]}` for crawlers.
- `GET /.well-known/mcp/server-card.json` — schema-1.0 card with full metadata.
- `GET /health` — liveness probe.

## Find more MCP servers for physical equipment

→ **https://physicalmcp.vercel.app** — the MCP directory for the physical world. Industrial, robotics, energy, fleet, buildings. We curate every server we can find, with honest descriptions and install snippets.

## Contact

Issues, custom-OEM normalization requests, MINT integration help, enterprise inquiries: **foundrynet@proton.me**.

## License

Commercial. Free tier available — see [LICENSE](./LICENSE) for the short version and https://foundrynet.io/pricing for the long version.
