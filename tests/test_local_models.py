"""Model picker: hardware sizing, Ollama downloads, switching and the job test."""
import importlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from warden import local_models, models


class FakeOllama(BaseHTTPRequestHandler):
    """Ollama's native API plus its OpenAI-style chat, in-process."""
    state = {}

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        s = self.state
        if self.path == "/api/version" and s["native"]:
            return self._json({"version": "0.9.0"})
        if self.path == "/api/tags" and s["native"]:
            return self._json({"models": [{"name": n, "size": 2_000_000_000} for n in s["installed"]]})
        if self.path == "/v1/models":
            return self._json({"data": [{"id": n} for n in s["installed"]]})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        s, body = self.state, self._body()
        if self.path == "/api/pull":
            self.send_response(200)
            self.end_headers()
            for done in (0, 50, 100):
                self.wfile.write(json.dumps({"status": "pulling", "completed": done, "total": 100}).encode() + b"\n")
            self.wfile.write(b'{"status":"success"}\n')
            s["installed"].append(body["model"])
            return
        if self.path == "/v1/chat/completions":
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            if system.startswith("Pick"):
                answer = "<think>The second chair has back support, so 2.</think>2"
            elif "JSON" in system:
                answer = json.dumps({"type": "monitor", "query": "propane grill", "min_discount_pct": 25})
            else:
                answer = "toys"
            return self._json({"choices": [{"message": {"content": answer}}]})
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        self.state["installed"].remove(self._body()["model"])
        self._json({})


@pytest.fixture
def ollama(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    FakeOllama.state = {"native": True, "installed": ["llama3.2:1b"]}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(models, "LOCAL_URL", f"http://127.0.0.1:{srv.server_port}/v1")
    before = models.LOCAL_MODEL
    models.set_local_model("llama3.2:1b")
    local_models.PULLS.clear()
    yield FakeOllama.state
    models.set_local_model(before)
    srv.shutdown()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDEN_DATA", str(tmp_path))
    import warden.server as server
    server = importlib.reload(server)
    return server, TestClient(server.app, base_url="http://127.0.0.1:8787",
                              headers={"X-Warden-Owner": server.OWNER_TOKEN})


def wait_for(pred, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_recommendation_sized_to_memory():
    assert local_models.recommend(0.5) is None
    assert local_models.recommend(1.5) == "llama3.2:1b"
    assert local_models.recommend(4) == "gemma3:4b"
    assert local_models.recommend(64) == "llama3.1:8b"  # bigger doesn't help Warden's small jobs
    hw = local_models.hardware()
    assert hw["kind"] in ("apple", "nvidia", "cpu") and hw["ram_gb"] > 0


def test_overview_marks_installed(ollama):
    o = local_models.overview()
    assert o["engine"] == {"kind": "ollama", "running": True, "version": "0.9.0"}
    assert o["active_installed"] and [m["name"] for m in o["installed"]] == ["llama3.2:1b"]
    assert next(m for m in o["catalog"] if m["name"] == "llama3.2:1b")["installed"]


def test_pull_then_use_then_delete(ollama, api):
    server, c = api
    ollama["installed"].clear()  # the active model isn't downloaded yet
    assert c.post("/api/models/pull", json={"name": "qwen2.5:3b"}).json()["status"] == "downloading"
    assert wait_for(lambda: local_models.PULLS.get("qwen2.5:3b", {}).get("status") == "success")
    assert wait_for(lambda: models.LOCAL_MODEL == "qwen2.5:3b")  # picked up automatically
    assert server.hub.policy.local_model == "qwen2.5:3b"
    assert c.post("/api/models/use", json={"name": "llama3.2:1b"}).json()["active"] == "llama3.2:1b"
    assert models.LOCAL_MODEL == "llama3.2:1b"
    assert c.post("/api/models/delete", json={"name": "qwen2.5:3b"}).json()["ok"]
    assert ollama["installed"] == []
    kinds = [e["kind"] for e in server.hub.ledger.list()]
    assert {"model_download_started", "model_deleted", "policy_changed"} <= set(kinds)


def test_model_api_is_owner_only_and_validates(ollama, api):
    server, c = api
    agent = {"Authorization": f"Bearer {server.AGENT_TOKEN}", "X-Warden-Owner": ""}
    assert c.post("/api/models/use", json={"name": "x"}, headers=agent).status_code == 401
    assert c.post("/api/models/pull", json={"name": "../../etc"}).status_code == 400
    assert c.post("/api/models/use", json={"name": "bad name; rm -rf"}).status_code == 400


def test_job_test_credits_the_model(ollama, api):
    _, c = api
    r = c.post("/api/models/test").json()
    assert r["model"] == "llama3.2:1b"
    assert [x["ok"] for x in r["results"]] == [True, True, True]
    assert r["results"][1]["answer"] == "Ergonomic Mesh Office Chair"  # <think> text ignored
    assert r["results"][2]["answer"] == "toys"


def test_no_engine_reports_failures_fast(api, monkeypatch):
    _, c = api
    monkeypatch.setattr(models, "LOCAL_URL", "http://127.0.0.1:9/v1")
    models._status_cache["v"] = None
    t = time.time()
    r = c.post("/api/models/test").json()
    assert not any(x["ok"] for x in r["results"]) and time.time() - t < 5
    assert c.get("/api/models").json()["engine"]["kind"] == "none"
    assert c.post("/api/models/pull", json={"name": "qwen2.5:3b"}).status_code == 409


def test_lm_studio_style_server_lists_but_cannot_pull(ollama, api):
    _, c = api
    ollama["native"] = False
    models._status_cache["v"] = None
    o = c.get("/api/models").json()
    assert o["engine"]["kind"] == "openai" and [m["name"] for m in o["installed"]] == ["llama3.2:1b"]
    assert c.post("/api/models/pull", json={"name": "qwen2.5:3b"}).status_code == 409


def test_cpu_only_says_slow_not_too_big(ollama, monkeypatch):
    monkeypatch.setattr(local_models, "hardware", lambda: {"kind": "cpu", "label": "CPU only · 16 GB RAM",
                                                           "ram_gb": 16, "budget_gb": 2.5})
    by_name = {m["name"]: m for m in local_models.overview()["catalog"]}
    assert by_name["llama3.2:3b"]["misfit"] is None and by_name["llama3.2:3b"]["recommended"]
    assert by_name["qwen2.5:7b"]["misfit"] == "slow without a GPU"
    assert by_name["gemma3:12b"]["misfit"] == "too big"


def test_memory_reported_like_macos(monkeypatch):
    sizes = {"SC_PAGE_SIZE": 16384, "SC_PHYS_PAGES": 24 * 1024 ** 3 // 16384}
    monkeypatch.setattr(local_models.os, "sysconf", lambda k: sizes[k])
    assert local_models._ram_gb() == 24.0  # a "24 GB" Mac, not 25.8


@pytest.fixture
def stopped_ollama(tmp_path, monkeypatch):
    """An installed-but-stopped Ollama: a fake `ollama` binary whose `serve`
    listens wherever OLLAMA_HOST says, like the real one."""
    import socket
    import subprocess
    import sys
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pidfile = tmp_path / "pid"
    fake = tmp_path / "ollama"
    fake.write_text(f"""#!{sys.executable}
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
open({str(pidfile)!r}, "w").write(str(os.getpid()))
host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({{"version": "0.12.3"}}).encode())
HTTPServer((host, int(port)), H).serve_forever()
""")
    fake.chmod(0o755)
    monkeypatch.setattr(models, "LOCAL_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(local_models, "ollama_binary", lambda: str(fake))
    monkeypatch.setenv("HOME", str(tmp_path))
    models._status_cache["v"] = None
    yield fake
    if pidfile.exists():
        subprocess.run(["kill", pidfile.read_text()])
    local_models.START.update(starting=False, error=None)


def test_stopped_ollama_started_from_dashboard(api, stopped_ollama):
    """`brew install ollama` doesn't start the server; Warden recognizes that and starts it."""
    _, c = api
    eng = c.get("/api/models").json()["engine"]
    assert eng["kind"] == "none" and eng["can_start"] and not eng["starting"]
    assert c.post("/api/models/start").json() == {"status": "running", "version": "0.12.3"}
    assert c.get("/api/models").json()["engine"]["running"]


def test_warden_starts_ollama_itself_at_launch(api, stopped_ollama, monkeypatch):
    server, c = api
    monkeypatch.setenv("WARDEN_START_OLLAMA", "1")
    with c:  # runs startup, like ./run.sh
        assert wait_for(lambda: local_models.engine()["running"], 15)
    assert wait_for(lambda: any(e["kind"] == "ollama_started" and e["actor"] == "hub"
                                for e in server.hub.ledger.list()))


def test_failed_start_explains_why(stopped_ollama):
    stopped_ollama.write_text("#!/bin/sh\necho 'Error: listen tcp 127.0.0.1:11434: bind: address already in use'\n")
    with pytest.raises(RuntimeError, match="address already in use"):
        local_models.start_ollama(wait=1)
    eng = local_models.engine()
    assert "address already in use" in eng["start_error"] and not eng["starting"]


@pytest.mark.parametrize("env,url", [
    ({}, "http://localhost:11434/v1"),
    ({"OLLAMA_HOST": "0.0.0.0:11500"}, "http://127.0.0.1:11500/v1"),
    ({"OLLAMA_HOST": "http://127.0.0.1:11600"}, "http://127.0.0.1:11600/v1"),
    ({"OLLAMA_HOST": "127.0.0.1"}, "http://127.0.0.1:11434/v1"),
    ({"OLLAMA_HOST": "0.0.0.0:11500", "WARDEN_LOCAL_URL": "http://localhost:1234/v1"}, "http://localhost:1234/v1"),
])
def test_finds_ollama_where_it_listens(monkeypatch, env, url):
    for k in ("OLLAMA_HOST", "WARDEN_LOCAL_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert models._default_local_url() == url


def test_no_start_offer_without_ollama_or_for_remote_servers(monkeypatch):
    monkeypatch.setattr(models, "LOCAL_URL", "http://127.0.0.1:9/v1")
    models._status_cache["v"] = None
    monkeypatch.setattr(local_models, "ollama_binary", lambda: None)
    assert local_models.engine()["can_start"] is False
    monkeypatch.setattr(local_models, "ollama_binary", lambda: "/opt/homebrew/bin/ollama")
    monkeypatch.setattr(models, "LOCAL_URL", "http://192.0.2.1:9/v1")  # someone else's server
    monkeypatch.setattr(models, "local_status", lambda: {"reachable": False})
    assert local_models.engine()["can_start"] is False


def test_local_model_ignores_proxy_settings(ollama, monkeypatch):
    """A system/VPN proxy must not hide a running Ollama on this machine."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    models._status_cache["v"] = None
    assert local_models.engine()["running"] and models.local_status()["reachable"]
    assert models.local_chat("Pick one. Reply with its number only.", "x") == "2"
