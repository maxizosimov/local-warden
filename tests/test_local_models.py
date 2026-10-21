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
