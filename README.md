# Warden

Let AI agents shop for you without handing them your card. Warden runs on
your machine, checks every purchase against your rules, and hands the
checkout back to you. Small jobs run on a local model; nothing about your
money is decided by a model.

## How a request flows

```
"buy a chair, my back hurts"

 1. Understand  local model     → buy · "chair"                   no model? rules
 2. Pick        local model     → Ergonomic Mesh Chair $189.99    no model? cheapest
 3. Check       policy engine   → allow · ask · deny              never a model
 4. Approve     you             → only if your risk level asks
 5. Pay         you             → Warden hands you the cart, you pay at the store
                                  and mark it paid

 Every step is written to a tamper-evident log on your machine.
```

Open questions ("which office chair is best under $200?") can go to a
frontier model for research. It gets the question with your address, card
and email removed, has no tools, and can't buy anything.

The dashboard shows the same table live under **Who does what**, and every
request shows which model or rule handled each step.

## Run it

```bash
./run.sh
```

Or with Docker: `docker build -t warden . && docker run -p 127.0.0.1:8787:8787 -v warden-data:/data warden`.
For a free hosted copy that redeploys on every push, see
[docs/reference.md](docs/reference.md#hosting).

Open the link it prints (`http://127.0.0.1:8787/#owner=…`). It works with no
model, card or store account: a demo store and rule-based fallbacks run the
whole flow. Try `buy gel pens`, `buy the propane grill when it's 25% off`,
or `buy a $100 gift card` (denied: blocked category).

## Use a local model

Anything with an OpenAI-style chat API works. Warden checks for it every 30
seconds and falls back to rules when it's off.

```bash
ollama pull llama3.2 && ollama serve          # default, nothing to configure
# LM Studio, llama.cpp, vLLM:
export WARDEN_LOCAL_URL=http://localhost:1234/v1 WARDEN_LOCAL_MODEL=qwen2.5-7b-instruct
```

Optional frontier research: `export ANTHROPIC_API_KEY=…` (or
`WARDEN_FRONTIER_PROVIDER=openai` with `OPENAI_API_KEY`).

## Connect your agent

`./mcp.sh` lets any MCP client (Claude Desktop, Claude Code, Cursor) shop
through Warden. Keep `./run.sh` running, then:

```bash
claude mcp add warden -- /absolute/path/to/warden/mcp.sh
```

```json
{ "mcpServers": { "warden": { "command": "/absolute/path/to/warden/mcp.sh" } } }
```

Ask it for something, or give it a product link from any store. Its
requests show up in Warden with its reason attached. It can propose, never
approve, pay or change your rules. For plain HTTP agents, see
[docs/reference.md](docs/reference.md#api).

## You stay in control

- **Risk dial (0-4):** how much needs your approval, from everything (1, the
  default) to anything inside your limits (4).
- **Hard limits, at every level:** max per purchase, daily and monthly caps,
  blocked stores and categories (gift cards, crypto, firearms, subscriptions).
- **You pay.** Warden prepares the cart; nothing is charged by Warden. After
  you've paid 5 of an agent's proposals at a connected store unchanged,
  Warden offers to pay there itself.
- **Your data stays local.** Everything lives in `./data`.

## More

- [docs/reference.md](docs/reference.md): risk levels, settings, tokens,
  products from any website, API, code layout
- [docs/roadmap.md](docs/roadmap.md): from demo to real payments
- Tests: `pip install -r requirements-dev.txt && pytest`
