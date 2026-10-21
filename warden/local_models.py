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
START = {"starting": False, "error": None}  # Warden starting Ollama for you
_lock = threading.Lock()


# ------------------------------------------------------------ hardware ----
GIB = 1024 ** 3  # memory is sold in GiB: a "24 GB" Mac has 24 GiB


def _ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GIB
    except (ValueError, OSError, AttributeError):
        try:
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                      text=True, timeout=5).stdout) / GIB
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
    return models.local_opener.open(req, timeout=timeout)


OLLAMA_PATHS = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
                "/Applications/Ollama.app/Contents/Resources/ollama"]


def ollama_binary() -> str | None:
    """Where Ollama is installed on this computer, if it is."""
    found = shutil.which("ollama")
    return found or next((p for p in OLLAMA_PATHS if os.path.isfile(p)), None)


def _local_url() -> bool:
    return re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", models.LOCAL_URL) is not None


def engine() -> dict:
    try:
        with _ollama("GET", "/api/version", timeout=2) as r:
            return {"kind": "ollama", "running": True, "version": json.loads(r.read()).get("version", "")}
    except Exception:
        pass
    if models.local_status()["reachable"]:
        return {"kind": "openai", "running": True}
    # Installed (e.g. with brew) but its server isn't running: Warden starts it.
    return {"kind": "none", "running": False, "can_start": _local_url() and ollama_binary() is not None,
            "starting": START["starting"], "start_error": START["error"]}


def _log_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".warden-ollama.log")


def _last_log_line() -> str:
    try:
        with open(_log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 2000))
            lines = [ln.strip() for ln in f.read().decode("utf-8", "ignore").splitlines() if ln.strip()]
        return lines[-1][:200] if lines else ""
    except OSError:
        return ""


def start_ollama(wait: float = 20) -> dict:
    """Run `ollama serve` in the background, on the address Warden uses, detached so
    it keeps running after Warden stops (like the Ollama app does)."""
    binary = ollama_binary()
    if not binary or not _local_url():
        raise RuntimeError("Ollama isn't installed on this computer.")
    hostport = re.sub(r"^https?://|/v1/?$", "", models.LOCAL_URL.rstrip("/"))
    START.update(starting=True, error=None)
    try:
        with open(_log_path(), "ab") as log:
            subprocess.Popen([binary, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             env={**os.environ, "OLLAMA_HOST": hostport}, start_new_session=True)
        end = time.time() + wait
        while time.time() < end:
            eng = engine()
            if eng["running"]:
                models._status_cache["v"] = None
                return eng
            time.sleep(0.25)
        why = _last_log_line()
        START["error"] = f"Ollama didn't start{': ' + why if why else ''}. Details in {_log_path()}"
        raise RuntimeError(START["error"])
    finally:
        START["starting"] = False


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
