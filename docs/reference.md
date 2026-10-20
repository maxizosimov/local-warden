# Reference

## Who does what

Defined in `warden/models.py` (`ROLES`) and shown in the dashboard.

| Job | Handled by | If unavailable | Code |
|---|---|---|---|
| Understand your request | local model | rules | `models.parse_intent` |
| Pick the best match | local model | cheapest in stock | `models.pick_product` |
| Tell the product category | rules, then local model | `unknown`: you approve | `categories.classify` |
| Research open questions | frontier model (redacted, no tools) | local model | `models.frontier_research` |
| Allow, ask or deny | policy engine | - | `policy.evaluate` |
| Approve and pay | you | - | dashboard |

Rules always win for gift cards, crypto, firearms and subscriptions, so a
product page or model answer can't talk its way past a blocked category.

## Risk dial

| Level | Name | Goes ahead without asking |
|---|---|---|
| 0 | Observe | Nothing. Research and price watches only. |
| 1 | Ask always | Nothing (default). |
| 2 | Guarded | Under the auto-approve limit at an approved store. |
| 3 | Trusted | Under the auto-approve limit at any store that isn't blocked. |
| 4 | Autonomous | Anything inside the hard limits. |

"Goes ahead" means the cart is prepared for you to pay. Two things always ask,
even at 4: a price Warden couldn't verify, and an unknown category. Your
approval overrides the dial, never the hard limits. If the price rises more
than 2% between the decision and checkout, the purchase is cancelled.

## Paying and the track record

Every allowed purchase lands under **Needs you** with a link to the cart or
product page. You pay there, then:

| You do | Recorded as |
|---|---|
| Mark paid, within 1% of the proposal | `as proposed` |
| Mark paid, different amount | `changed` (the real amount counts toward your caps) |
| Skip | `skipped` (its budget is released) |

Open handoffs reserve budget, so an agent can't stack proposals past your caps.
When your last 5 decided proposals at a connected store were all paid as
proposed, Warden offers **Let Warden pay** there (adds it to `pay_merchants`).
Payments use a mock single-use token in this version.

## Products from any website

Agents can send a product link from any store. The store is the site's domain
(`www.staples.com` → `staples.com`; listing `staples.com` also covers
subdomains).

- **Price:** read from the page (schema.org data or price meta tags). If the
  store blocks that, the agent's price is used, marked *price unverified*, and
  you always approve it. If the page later shows a higher price, the purchase
  is cancelled.
- **Paying:** always you. Warden never pays on websites it isn't connected to.
- **Safety:** only public http(s) pages are fetched; no `file://`, localhost
  or private network addresses.

## Settings

All in `data/policy.json`, editable from the dashboard (**Hard limits**).

| Setting | Default |
|---|---|
| `risk_level` | 1 |
| `max_per_transaction` | 300 |
| `auto_approve_under` | 50 |
| `daily_cap` / `monthly_cap` | 200 / 600 |
| `allowed_merchants` | `["demo-store"]` |
| `blocked_merchants` | `[]` |
| `blocked_categories` | gift_cards, crypto, firearms, subscriptions |
| `pay_merchants` | `[]` (stores where Warden pays) |
| `frontier_research_allowed` / `frontier_redact_pii` | true / true |

| Environment | |
|---|---|
| `WARDEN_LOCAL_URL`, `WARDEN_LOCAL_MODEL` | Local model (default Ollama, `llama3.2`) |
| `ANTHROPIC_API_KEY` or `WARDEN_FRONTIER_PROVIDER=openai` + `OPENAI_API_KEY` | Frontier research |
| `WARDEN_WEBHOOK_URL` | Push approvals and alerts to ntfy.sh, Slack, etc. |
| `WARDEN_HOST`, `WARDEN_PORT`, `WARDEN_DATA` | Where the hub listens and stores data |

## Tokens

| Token | File | Can |
|---|---|---|
| Agent | `data/agent_token` | Propose purchases, set price watches, read policy, catalog, feed and track record |
| Owner | `data/owner_token` | Everything: approve, mark paid, change settings |

Give agents the agent token only. The MCP server refuses to run with the owner
token. The hub sets who a request came from itself, only answers on localhost
hostnames, and the dashboard sends the owner token in a header other websites
can't send.

Known limit: an agent that can read your files can read `data/owner_token`.
The fix is approvals signed on your phone (see the roadmap).

## API

Agent token:

```bash
curl -X POST localhost:8787/api/intents -H "Authorization: Bearer $(cat data/agent_token)" \
  -H "Content-Type: application/json" \
  -d '{"sku":"PEN-510","reason":"office restock"}'          # connected store
#    '{"url":"https://…","price":129.99,"name":"…","reason":"…"}'  # any website
```

| Endpoint | Token | |
|---|---|---|
| `POST /api/intents` | agent | Propose a purchase: `awaiting_approval`, `handed_off`, `ordered`, `denied` |
| `GET /api/approvals/{id}`, `GET /api/orders/{id}` | agent | Follow up on a request |
| `POST /api/monitors` | agent | Price watch (`max_price` or `min_discount_pct`, `action` buy/notify) |
| `GET /api/policy`, `/api/catalog`, `/api/track-record` | agent | Read |
| `GET /api/feed?since_id=N` | agent | Every log event after N |
| `POST /api/approvals/{id}/approve` or `/reject` | owner | Decide |
| `POST /api/orders/{id}/paid` `{"amount": 14.49}`, `/skip` | owner | Record payment |
| `PUT /api/policy` | owner | Change settings |
| `GET /api/ledger/verify`, `/api/export/orders.csv` | agent / owner | Check the log, export orders |

## Code layout

```
warden/
  models.py        who does what: local model jobs, rule fallbacks, frontier research
  categories.py    product categories (rules first, then local model)
  policy.py        risk dial and hard limits (pure function)
  hub.py           the flow: request → pick → policy → approval → handoff/order
  ledger.py        append-only, hash-chained log
  vault.py         single-use payment tokens (mock)
  merchants/       demo-store (buyable), web (any site: price reader, handoff only)
  server.py        HTTP API and dashboard (static/index.html)
  mcp_server.py    MCP tools for agents (agent token only)
```
