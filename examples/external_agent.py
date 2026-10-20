"""Example: plug ANY agent into Warden.

Your agent (Ollama script, LangChain, OpenClaw, Claude Code, a cron job...)
never holds a card. It asks the hub, and the hub applies your policy,
mints a single-use token, places the order and logs everything.

    python examples/external_agent.py CHR-200
"""
import json
import sys
import urllib.request
from pathlib import Path

HUB = "http://127.0.0.1:8787"
TOKEN = Path("data/agent_token").read_text().strip()


def ask_hub(sku: str, qty: int = 1, merchant: str = "demo-store") -> dict:
    req = urllib.request.Request(
        f"{HUB}/api/intents",
        data=json.dumps({"merchant": merchant, "sku": sku, "quantity": qty}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    return json.loads(urllib.request.urlopen(req).read())


if __name__ == "__main__":
    r = ask_hub(sys.argv[1] if len(sys.argv) > 1 else "PEN-510")
    print(json.dumps({k: r.get(k) for k in ("status", "message", "approval_id", "order_id", "decision")}, indent=2))
