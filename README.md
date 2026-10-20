# Warden — a local, policy-first hub for agent purchases

Your agent can shop. Warden decides whether it's allowed to, holds the
payment method so the agent never does, and writes every step to a
tamper-evident ledger on your own machine.

```
 you / any agent ──► Warden hub (localhost) ──────────────────────────────►  merchant
   "buy the grill      │ 1. local model parses the request                    adapters
    when 25% off"      │ 2. policy engine + risk dial: allow / ask / deny
                       │ 3. vault mints a single-use, merchant-locked token
 frontier model ◄──────│    (research only: redacted text in, text out,
 (optional)            │     no tools, can never trigger a purchase)
                       └ 4. every step → hash-chained audit ledger
```

## Run it (2 minutes)

```bash
./run.sh                 # or: pip install -r requirements.txt && python -m warden.server
open http://127.0.0.1:8787
```

No model, card or merchant account needed: a rule-based parser and a demo
store with sales you can trigger make the whole loop testable out of the box.

### Try these
| Type in the box | What happens |
|---|---|
| `buy a chair from staples` | Finds a chair, asks for approval (risk level 1). Staples isn't connected, so it's simulated with the demo store and labeled as such. |
| `buy gel pens` at risk level 2 | $14 at an approved merchant: auto-approved and ordered. |
| `buy the propane grill when it's 25% off` | Creates a monitor. Click **30% sale** on the grill in the demo store and it fires, then goes through policy. |
| `tell me when the standing desk drops below $200` | Notify-only monitor. |
| `find me the best office chair under 200, ship to 12 Oak St` | Research mode. Address is redacted before anything leaves the machine. Nothing is bought. |
| `buy a $100 gift card` | Denied: gift cards are a blocked category at every risk level. |

## Plug in a local model

Anything that speaks the OpenAI chat API:

```bash
ollama pull llama3.2 && ollama serve                     # default: http://localhost:11434/v1
# or LM Studio / llama.cpp / vLLM:
export WARDEN_LOCAL_URL=http://localhost:1234/v1
export WARDEN_LOCAL_MODEL=qwen2.5-7b-instruct
```

Optional frontier research (never touches payments):

```bash
export ANTHROPIC_API_KEY=...            # or WARDEN_FRONTIER_PROVIDER=openai + OPENAI_API_KEY
export WARDEN_FRONTIER_MODEL=claude-sonnet-5-5
```

## Risk dial

| Level | Name | Behavior |
|---|---|---|
| 0 | Observe | Research and monitors only. No purchases. |
| 1 | Ask always | Every purchase waits for approval. (default) |
| 2 | Guarded | Under the auto-approve limit **and** at an approved merchant → auto. Else ask. |
| 3 | Trusted | Under the auto-approve limit at any non-blocked merchant → auto. |
| 4 | Autonomous | Anything inside hard limits → auto. |

**Hard limits apply at every level**: per-purchase max, daily and monthly
caps, blocked merchants and categories, and "frontier models can't buy".
A human approval overrides the dial, never the hard limits. If the price
rises more than 2% between decision and checkout, the order is aborted.

## Connect your own agent

Any agent can request a purchase; it never gets card details.

```bash
curl -X POST localhost:8787/api/intents \
  -H "Authorization: Bearer $(cat data/agent_token)" \
  -H "Content-Type: application/json" \
  -d '{"merchant":"demo-store","sku":"PEN-510","quantity":1}'
```

The hub looks up the live price itself (the agent can't claim a price),
applies policy and replies `ordered`, `awaiting_approval` or `denied`.
See `examples/external_agent.py`.

## Data feed

| Endpoint | |
|---|---|
| `GET /api/feed?since_id=N` | Every ledger event after N (poll it) |
| `GET /api/ledger/verify` | Re-hashes the whole chain; reports the first tampered row |
| `GET /api/export/orders.csv` | Orders for your spreadsheet / accounting |
| `WARDEN_WEBHOOK_URL` | Push approvals/alerts to ntfy.sh, Slack, etc. |

All state lives in `./data` (SQLite + `policy.json`). Nothing leaves the
machine except optional redacted research questions.

## Layout

```
warden/
  policy.py        risk dial + hard limits (pure function, easy to audit)
  ledger.py        append-only, hash-chained audit log
  vault.py         single-use merchant-locked payment tokens (mock)
  models.py        local model router, rule parser, PII redaction, frontier research
  hub.py           orchestration: tasks, approvals, execution, monitors, orders
  merchants/       adapters: demo-store (buyable), web (read-only price reader)
  server.py        FastAPI + dashboard (static/index.html)
tests/test_core.py
```

## From MVP to something people pay $5/mo for

1. **Real payments** — implement `vault.py` against an issuer of single-use,
   merchant-locked virtual cards (Lithic, Stripe Issuing, Privacy.com). The
   policy decision maps straight onto card controls (merchant lock + amount cap),
   so even a compromised agent can't overspend.
2. **Real merchants** — adapters per store: official APIs where they exist,
   otherwise a local Playwright checkout using the user's own logged-in browser
   profile. Keep `place_order` behind the executor only.
3. **Order tracking** — parse order/shipping emails (local IMAP + local model)
   instead of scraping merchant sites.
4. **MCP server** — expose `search`, `request_purchase`, `create_monitor` as MCP
   tools so Claude Desktop, Cursor, OpenClaw etc. plug in with zero code.
5. **Packaging** — a single desktop binary (Tauri/Electron) with phone push
   approvals. Charge for the hosted relay (push notifications, encrypted
   multi-device sync), not for the local core — that keeps the trust story intact.
