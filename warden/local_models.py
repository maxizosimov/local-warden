"""Pick, download and test the local model, sized to this computer.

Downloads go through Ollama's own API: it already runs models well on Mac,
Windows and Linux, so Warden doesn't ship an inference engine. Other
OpenAI-style servers (LM Studio, llama.cpp) still work: you pick from the
models they list, and download new ones in their own app.
"""
from __future__ import annotations

import functools
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from . import categories, models

# Small instruction models that do Warden's jobs well. Sizes are approximate downloads.
CATALOG = [
    {"name": "llama3.2:1b", "gb": 1.3, "note": "Fastest. Handles simple requests."},
    {"name": "qwen2.5:3b", "gb": 1.9, "note": "Follows Warden's answer formats well."},
    {"name": "llama3.2:3b", "gb": 2.0, "note": "Good all-rounder for 8 GB machines."},
    {"name": "gemma3:4b", "gb": 3.3, "note": "Better judgment when picking products."},
    {"name": "qwen2.5:7b", "gb": 4.7, "note": "Most reliable at structured answers."},
    {"name": "llama3.1:8b", "gb": 4.9, "note": "Most reliable. Best fit for 16 GB."},
    {"name": "gemma3:12b", "gb": 8.1, "note": "More than Warden's jobs need."},
]
ENOUGH_GB = 5.0  # bigger models don't do Warden's small jobs noticeably better
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

PULLS: dict[str, dict] = {}  # model name -> download progress
_lock = threading.Lock()


# ------------------------------------------------------------ hardware ----
def _ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        try:
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                      text=True, timeout=5).stdout) / 1e9
        except Exception:
            return 0.0


def _nvidia() -> tuple[str, float] | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.splitlines()[0]
        name, mib = out.rsplit(",", 1)
        return name.strip(), int(mib) / 1024
    except Exception:
        return None


@functools.cache
def hardware() -> dict:
    ram = round(_ram_gb(), 1)
    gpu = _nvidia()
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        kind, label = "apple", f"Apple Silicon · {ram:.0f} GB unified memory"
        budget = ram * 0.6  # shared with macOS and your apps
    elif gpu:
        kind, label = "nvidia", f"{gpu[0]} · {gpu[1]:.0f} GB VRAM · {ram:.0f} GB RAM"
        budget = gpu[1] * 0.9
    else:
        kind, label = "cpu", f"CPU only · {ram:.0f} GB RAM"
        budget = min(ram * 0.5, 2.5)  # without a GPU, only small models answer quickly
    return {"kind": kind, "label": label, "ram_gb": ram, "budget_gb": round(budget, 1)}


def recommend(budget_gb: float) -> str | None:
    fits = [m for m in CATALOG if m["gb"] <= min(budget_gb, ENOUGH_GB)]
    return max(fits, key=lambda m: m["gb"])["name"] if fits else None


# -------------------------------------------------------------- engine ----
def _native() -> str:
    """Ollama's own API root (the chat API lives under /v1)."""
    return re.sub(r"/v1/?$", "", models.LOCAL_URL.rstrip("/"))


def _ollama(method: str, path: str, body: dict | None = None, timeout: float = 5):
    req = urllib.request.Request(_native() + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def engine() -> dict:
    try:
        with _ollama("GET", "/api/version", timeout=2) as r:
            return {"kind": "ollama", "running": True, "version": json.loads(r.read()).get("version", "")}
    except Exception:
        pass
    if models.local_status()["reachable"]:
        return {"kind": "openai", "running": True}
    return {"kind": "none", "running": False}


def installed(kind: str) -> list[dict]:
    if kind == "ollama":
        with _ollama("GET", "/api/tags") as r:
            return [{"name": m["name"], "gb": round(m.get("size", 0) / 1e9, 1)}
                    for m in json.loads(r.read()).get("models", [])]
    if kind == "openai":
        return [{"name": n, "gb": None} for n in models.local_status().get("available", [])]
    return []


def _same(a: str, b: str) -> bool:
    """'llama3.2' and 'llama3.2:latest' are the same Ollama model."""
    def norm(n):
        return n if ":" in n else n + ":latest"
    return norm(a) == norm(b)


def overview() -> dict:
    eng = engine()
    have = installed(eng["kind"]) if eng["running"] else []
    hw = hardware()
    rec = recommend(hw["budget_gb"])
    active = models.LOCAL_MODEL
    def misfit(m):  # why a model isn't a good fit here, if it isn't
        if m["gb"] <= hw["budget_gb"]:
            return None
        return "slow without a GPU" if hw["kind"] == "cpu" and m["gb"] <= hw["ram_gb"] * 0.5 else "too big"
    catalog = [{**m, "fits": misfit(m) is None, "misfit": misfit(m), "recommended": m["name"] == rec,
                "installed": any(_same(m["name"], h["name"]) for h in have)} for m in CATALOG]
    with _lock:
        pulls = {k: dict(v) for k, v in PULLS.items()}
    return {"engine": eng, "url": models.LOCAL_URL, "hardware": hw, "recommended": rec,
            "active": active, "active_installed": any(_same(active, h["name"]) for h in have),
            "installed": have, "catalog": catalog, "pulls": pulls}


# ----------------------------------------------------------- downloads ----
def start_pull(name: str, on_done) -> None:
    """Download a model with Ollama in the background; progress lands in PULLS."""
    if not NAME_RE.match(name):
        raise ValueError("invalid model name")
    with _lock:
        if PULLS.get(name, {}).get("status") not in (None, "success", "error"):
            return  # already downloading
        PULLS[name] = {"status": "starting", "completed": 0, "total": 0}
    threading.Thread(target=_pull, args=(name, on_done), daemon=True).start()


def _pull(name: str, on_done) -> None:
    try:
        with _ollama("POST", "/api/pull", {"model": name, "name": name, "stream": True}, timeout=60) as r:
            for line in r:
                d = json.loads(line or b"{}")
                if d.get("error"):
                    raise RuntimeError(d["error"])
                with _lock:
                    p = PULLS[name]
                    p["status"] = d.get("status", p["status"])
                    p["completed"] = d.get("completed", p["completed"])
                    p["total"] = d.get("total", p["total"])
        with _lock:
            PULLS[name] = {"status": "success", "completed": 1, "total": 1}
        on_done(name)
    except Exception as e:
        with _lock:
            PULLS[name] = {"status": "error", "error": str(e)[:200]}


def delete(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError("invalid model name")
    _ollama("DELETE", "/api/delete", {"model": name, "name": name}).close()


# ---------------------------------------------------------------- test ----
def test_jobs(chairs: list) -> list[dict]:
    """Run Warden's three model jobs on samples. `ok` means the model itself
    answered usefully (not the rule fallback)."""
    out = []

    t = time.time()
    parsed, by = models.parse_intent("buy the propane grill when it's 25% off")
    out.append({"job": "Understand a request", "seconds": round(time.time() - t, 1),
                "ok": by != models.RULES and parsed["type"] == "monitor",
                "answer": f'{parsed["type"]} · "{parsed["query"]}"' + (f' · {parsed["min_discount_pct"]:g}% off'
                                                                       if parsed.get("min_discount_pct") else "")})
    t = time.time()
    i, by = models.pick_product("buy a chair, my back hurts", chairs)
    out.append({"job": "Pick the best match", "seconds": round(time.time() - t, 1),
                "ok": by != models.RULES + " · cheapest", "answer": chairs[i].name})
    t = time.time()
    cat = categories.model_category("Nerf Elite 2.0 Blaster")
    out.append({"job": "Tell the category", "seconds": round(time.time() - t, 1),
                "ok": cat is not None, "answer": cat or "no usable answer"})
    return out
