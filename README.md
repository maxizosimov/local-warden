# Warden — a local, policy-first hub for agent purchases

Your agent can shop. Warden decides whether it's allowed to, holds the
payment method so the agent never does, and writes every step to a
tamper-evident ledger on your own machine.

**It starts in handoff mode: the agent does everything up to checkout and
you pay.** Warden records whether you paid what the agent proposed. Once it
has a track record at a store, Warden offers to pay there itself.

```
 you / any agent ──► Warden hub (localhost) ──────────────────────────────►  merchant
   "buy the grill      │ 1. local model parses the request                    adapters
    when 25% off"      │ 2. policy engine + risk dial: allow / ask / deny
                       │ 3. handoff: cart ready, you pay  ─or─  vault mints a
                       │    single-use, merchant-locked token (trusted stores)
 frontier model ◄──────│    (research only: redacted text in, text out,
 (optional)            │     no tools, can never trigger a purchase)
                       └ 4. every step → hash-chained audit ledger
```

## Run it (2 minutes)

```bash
./run.sh                 # or: pip install -r requirements.txt && python -m warden.server
```

Open the link it prints (`http://127.0.0.1:8787/#owner=…`). The part after
`#` is your **owner token**; the dashboard keeps it in the browser.

No model, card or merchant account needed: a rule-based parser and a demo
store with sales you can trigger make the whole loop testable out of the box.

### Try these
| Type in the box | What happens |
|---|---|
| `buy a chair from staples` | Finds a chair, asks for approval (risk level 1). Staples isn't connected, so it's simulated with the demo store and labeled as such. |
| `buy gel pens` at risk level 2 | $14 at an approved merchant: allowed, cart handed to you. **Open cart**, then **Mark paid** (edit the amount if you were charged something else). |
| `buy the propane grill when it's 25% off` | Creates a monitor. Click **30% sale** on the grill in the demo store and it fires, then goes through policy. |
| `tell me when the standing desk drops below $200` | Notify-only monitor. |
| `find me the best office chair under 200, ship to 12 Oak St` | Research mode. Address is redacted before anything leaves the machine. Nothing is bought. |
| `buy a $100 gift card` | Denied: gift cards are a blocked category at every risk level. |

## Handoff mode and the track record

Nothing is charged by Warden until you say so. Every allowed purchase lands
in **Ready for you to pay** with the cart link, expected total and the policy
reasons. You pay at the store and record it:

| You do | Recorded as |
|---|---|
| Mark paid, within 1% of the proposal | `as proposed` |
| Mark paid, different amount | `changed` (the real amount replaces the estimate in your spend) |
| Skip | `skipped` (its budget is released) |

Open handoffs reserve budget against the daily and monthly caps, so the agent
can't stack proposals past your limits.

**Trust is earned per merchant.** When your last 5 decided proposals at a
store were all paid as proposed, the track record panel offers **Let Warden
pay**. That adds the store to `pay_merchants`; purchases there use a
single-use token instead of a handoff. `payment_mode: "pay"` turns it on
everywhere. In this MVP the vault is a mock, so "Warden pays" is simulated.

| Endpoint (owner token) | |
|---|---|
| `POST /api/orders/{id}/paid` `{"amount": 14.49}` | Record what you were charged |
| `POST /api/orders/{id}/skip` | Decline the proposal |
| `GET /api/track-record` | Overall and per-merchant record + suggestions (agent token works too) |

## Two tokens

| Token | File | Can |
|---|---|---|
| Agent | `data/agent_token` | Submit purchase intents; read the feed, catalog, policy and track record |
| Owner | `data/owner_token` | Everything: approve, mark paid, change policy and the risk dial |

Give your agent the agent token, never the owner token. The hub sets each
intent's `source` itself, so an agent can't claim to be you. The API only
answers on localhost hostnames (blocks DNS rebinding), and the dashboard
sends the owner token in a custom header, which other websites can't do.

**Known limit:** an agent that can read your files can read
`data/owner_token` and edit `data/`. Local software can't fully guard against
a process running as the same user. The fix is on the roadmap below:
approvals signed on your phone, and spending limits enforced by the card issuer.

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
| `GET /api/feed?since_id=N` | Every ledger event after N (poll it; agent or owner token) |
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
tests/            core, handoff and API-auth tests (pip install -r requirements-dev.txt; pytest)
```

## From MVP to something people pay $5/mo for

1. **Real payments** — implement `vault.py` against an issuer of single-use,
   merchant-locked virtual cards (Lithic, Stripe Issuing, Privacy.com). The
   policy decision maps straight onto card controls (merchant lock + amount cap),
   and the issuer's real-time authorization webhook lets policy approve or
   decline each charge, so even a compromised agent can't overspend. Graduate
   from handoff to these cards, never to the user's real card or bank login.
2. **Real merchants** — adapters per store: official APIs where they exist,
   otherwise a local Playwright checkout using the user's own logged-in browser
   profile. Keep `place_order` behind the executor only.
3. **Order tracking** — parse order/shipping emails (local IMAP + local model)
   instead of scraping merchant sites.
4. **MCP server** — expose `search`, `request_purchase`, `create_monitor` as MCP
   tools so Claude Desktop, Cursor, OpenClaw etc. plug in with zero code.
   Their proposals flow into the same handoff queue and track record.
5. **Approvals off the machine** — sign approvals on your phone, and anchor
   the ledger head outside the machine, so a local agent can't approve its
   own purchases or rewrite history.
6. **Packaging** — a single desktop binary (Tauri/Electron) with phone push
   approvals. Charge for the hosted relay (push notifications, encrypted
   multi-device sync), not for the local core — that keeps the trust story intact.
