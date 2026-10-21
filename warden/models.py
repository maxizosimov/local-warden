"""Which model does what.

Small, private jobs go to your LOCAL model (Ollama, LM Studio, llama.cpp, vLLM,
anything with an OpenAI-style chat API). Each has a rule-based fallback, so
Warden works with no model at all. Open-ended research can go to a FRONTIER
model, but only as redacted text with no tools. No model ever decides whether
a purchase is allowed or pays: that's the policy engine and you.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

def _default_local_url() -> str:
    """WARDEN_LOCAL_URL, else wherever Ollama is told to listen (OLLAMA_HOST), else its default."""
    if os.getenv("WARDEN_LOCAL_URL"):
        return os.environ["WARDEN_LOCAL_URL"]
    host = os.getenv("OLLAMA_HOST", "").strip()
    if not host:
        return "http://localhost:11434/v1"
    p = urllib.parse.urlsplit(host if "://" in host else "http://" + host)
    name = "127.0.0.1" if p.hostname in (None, "0.0.0.0", "::") else p.hostname
    return f"{p.scheme}://{name}:{p.port or 11434}/v1"


LOCAL_URL = _default_local_url()
LOCAL_MODEL = os.getenv("WARDEN_LOCAL_MODEL", "llama3.2")
FRONTIER_PROVIDER = os.getenv("WARDEN_FRONTIER_PROVIDER", "anthropic")  # anthropic | openai
FRONTIER_MODEL = os.getenv("WARDEN_FRONTIER_MODEL", "claude-sonnet-5-5")


# Shown in the dashboard and README. Keep in sync with the functions below.
ROLES = [
    {"task": "Understand your request", "by": "local", "fallback": "rules", "fn": "parse_intent"},
    {"task": "Pick the best match", "by": "local", "fallback": "cheapest in stock", "fn": "pick_product"},
    {"task": "Tell the product category", "by": "rules, then local", "fallback": "unknown: you approve",
     "fn": "categories.classify"},
    {"task": "Research open questions", "by": "frontier (redacted, no tools)", "fallback": "local",
     "fn": "frontier_research"},
    {"task": "Allow, ask or deny", "by": "policy engine", "fallback": None, "fn": "policy.evaluate"},
    {"task": "Approve and pay", "by": "you", "fallback": None, "fn": None},
]

RULES = "rules"


def local_label() -> str:
    return f"local · {LOCAL_MODEL}"


def set_local_model(name: str) -> None:
    """Switch the local model (from the dashboard's model picker)."""
    global LOCAL_MODEL
    LOCAL_MODEL = name
    _status_cache["v"] = None


# The local model is on this machine: never route it through a system or VPN proxy
# (macOS proxy settings can otherwise catch localhost and hide a running Ollama).
local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url: str, body: dict, headers: dict, timeout: int = 60, local: bool = False) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}
    )
    opener = local_opener if local else urllib.request.build_opener()
    return json.loads(opener.open(req, timeout=timeout).read())


# ---------------------------------------------------------------- local ----
def local_chat(system: str, user: str, timeout: int = 60) -> str | None:
    """One-shot chat with the local model, or None if it isn't running."""
    if not local_status()["reachable"]:
        return None
    try:
        r = _post(
            f"{LOCAL_URL.rstrip('/')}/chat/completions",
            {"model": LOCAL_MODEL, "temperature": 0,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            {"Authorization": f"Bearer {os.getenv('WARDEN_LOCAL_KEY', 'local')}"},
            timeout, local=True,
        )
        text = r["choices"][0]["message"]["content"] or ""
        # Reasoning models think out loud first; only the answer after it counts.
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    except Exception:
        return None


_status_cache: dict = {"t": 0.0, "v": None}


def local_status() -> dict:
    import time
    if _status_cache["v"] and time.time() - _status_cache["t"] < 10:
        return _status_cache["v"]
    _status_cache.update(t=time.time(), v=_local_status())
    return _status_cache["v"]


def _local_status() -> dict:
    try:
        req = urllib.request.Request(f"{LOCAL_URL.rstrip('/')}/models")
        data = json.loads(local_opener.open(req, timeout=3).read())
        return {"reachable": True, "url": LOCAL_URL, "model": LOCAL_MODEL,
                "available": [m.get("id") for m in data.get("data", [])][:20]}
    except Exception:
        return {"reachable": False, "url": LOCAL_URL, "model": LOCAL_MODEL,
                "note": "Not running: rules handle its jobs. Start Ollama or LM Studio to use it."}


# ------------------------------------------------------------- intent -----
INTENT_SYSTEM = """You turn a shopping request into JSON. Reply with JSON only.
Schema: {"type": "buy"|"monitor"|"research",
 "query": short product search terms,
 "merchant": store name or null,
 "quantity": int,
 "max_price": number or null,
 "min_discount_pct": number or null,
 "action_on_trigger": "buy"|"notify",
 "needs_research": bool}
"monitor" = wait for a price/sale condition. "research" = user wants options, not a purchase."""


def rule_parse(text: str) -> dict:
    t = text.lower().strip()
    # drop delivery details before parsing so addresses aren't read as quantities/products
    t = re.sub(r"[,;]?\s*\b(ship|deliver|send)(ped|ing)?\s+(it\s+)?to\b.*$", "", t)
    out = {"type": "buy", "query": "", "merchant": None, "quantity": 1, "max_price": None,
           "min_discount_pct": None, "action_on_trigger": "buy", "needs_research": False}

    if m := re.search(r"(\d+)\s*%\s*off|(\d+)\s*percent", t):
        out["min_discount_pct"] = float(m.group(1) or m.group(2))
    if m := re.search(r"(?:under|below|less than|<|max|at most|for)\s*\$?\s*(\d+(?:\.\d+)?)", t):
        out["max_price"] = float(m.group(1))
    if m := re.search(r"\b(?:from|at|on)\s+([a-z][\w&'.-]+)", t):
        if m.group(1) not in {"most", "least", "sale", "the", "a"}:
            out["merchant"] = m.group(1)
    if m := re.search(r"\b(?:buy|get|order|purchase)\s+(\d{1,2})\s+(?!%|percent|dollars)[a-z]", t) \
            or re.search(r"\b(\d{1,2})\s*(?:x|units?|pcs|pieces)\b", t):
        out["quantity"] = int(m.group(1))

    if re.search(r"\bwhen\b|\bif\b|\bon sale\b|\bdrops?\b|\bwatch\b|\bmonitor\b|\bonce\b", t):
        out["type"] = "monitor"
        if re.search(r"\b(tell|notify|alert|let me know|ping)\b", t):
            out["action_on_trigger"] = "notify"
        if out["min_discount_pct"] is None and out["max_price"] is None and "sale" in t:
            out["min_discount_pct"] = 15.0
    elif re.search(r"\b(best|compare|recommend|research|which|options|find me)\b", t):
        out["type"] = "research"
        out["needs_research"] = True

    q = re.sub(r"\b(buy|get|order|purchase|me|a|an|the|please|new|when|if|it'?s|is|on|sale|"
               r"from|at|for|under|below|less|than|goes|drops?|to|watch|monitor|tell|notify|"
               r"let|know|alert|once|find|best|recommend|compare|research|which|options|"
               r"some|i|want|need|percent|off|max|most|least|and|or|by|of|cheap|good)\b", " ", t)
    q = re.sub(r"\$?\d+(\.\d+)?%?|[^a-z\s-]", " ", q)
    if out["merchant"]:
        q = q.replace(out["merchant"], " ")
    out["query"] = " ".join(q.split()) or text
    return out


def parse_intent(text: str) -> tuple[dict, str]:
    raw = local_chat(INTENT_SYSTEM, text, timeout=30)
    if raw:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                base = rule_parse(text)
                base.update({k: v for k, v in d.items() if k in base and v not in ("", None)})
                return base, local_label()
            except json.JSONDecodeError:
                pass
    return rule_parse(text), RULES


# --------------------------------------------------------------- pick ------
PICK_SYSTEM = "Pick the product that best matches the shopping request. Reply with its number only."


def pick_product(request: str, candidates: list) -> tuple[int, str]:
    """Index of the best match among `candidates` (cheapest first) and who chose it."""
    shown = candidates[:10]
    if len(shown) > 1:
        listing = "\n".join(f"{i + 1}. {p.name} - ${p.price:.2f}" for i, p in enumerate(shown))
        raw = local_chat(PICK_SYSTEM, f"Request: {request}\n\n{listing}", timeout=30)
        m = re.search(r"\d+", raw or "")
        if m and 1 <= int(m.group()) <= len(shown):
            return int(m.group()) - 1, local_label()
    return 0, f"{RULES} · cheapest"


# ------------------------------------------------------------ frontier ----
PII_PATTERNS = [
    (r"\b(?:\d[ -]?){13,19}\b", "[CARD]"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]"),
    (r"\+?\d?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", "[PHONE]"),
    (r"\b\d{1,5}\s+\w+(?:\s\w+)?\s(?:st|street|ave|avenue|rd|road|blvd|ln|lane|dr|drive|ct|way)\b", "[ADDRESS]"),
    (r"\b\d{5}(?:-\d{4})?\b", "[ZIP]"),
]


def redact(text: str) -> str:
    for pat, rep in PII_PATTERNS:
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def frontier_status() -> dict:
    key = os.getenv("ANTHROPIC_API_KEY") if FRONTIER_PROVIDER == "anthropic" else os.getenv("OPENAI_API_KEY")
    return {"configured": bool(key), "provider": FRONTIER_PROVIDER, "model": FRONTIER_MODEL,
            "role": "research only - no tools, cannot purchase"}


def frontier_research(question: str, redact_pii: bool = True) -> tuple[str, str]:
    """Returns (answer, question_actually_sent). No tools are passed, ever."""
    sent = redact(question) if redact_pii else question
    system = ("You are a shopping research assistant. Recommend specific product types, "
              "key specs, fair price ranges and what to avoid. Be concise. End with a line "
              "'SEARCH: <3-6 word search query>' for the best pick.")
    try:
        if FRONTIER_PROVIDER == "anthropic":
            key = os.environ["ANTHROPIC_API_KEY"]
            r = _post("https://api.anthropic.com/v1/messages",
                      {"model": FRONTIER_MODEL, "max_tokens": 800, "system": system,
                       "messages": [{"role": "user", "content": sent}]},
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
            return "".join(b.get("text", "") for b in r["content"]), sent
        key = os.environ["OPENAI_API_KEY"]
        r = _post("https://api.openai.com/v1/chat/completions",
                  {"model": FRONTIER_MODEL, "messages": [{"role": "system", "content": system},
                                                         {"role": "user", "content": sent}]},
                  {"Authorization": f"Bearer {key}"})
        return r["choices"][0]["message"]["content"], sent
    except KeyError:
        pass
    except Exception as e:  # network / auth
        return f"(frontier research failed: {e})", sent
    # Fallback: let the local model research if no frontier key.
    ans = local_chat(system, sent)
    return (ans or "(No frontier key set and no local model running - showing catalog matches only.)"), sent
