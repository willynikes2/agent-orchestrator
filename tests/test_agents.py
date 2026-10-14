"""Tests for the Grok / Antigravity / Argon adapters. No real CLI or network is ever touched."""
import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

BYPASS_FLAGS = ("--always-approve", "bypassPermissions", "--dangerously-skip-permissions")


class Capture:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.calls = []
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, cmd, cwd, env, timeout, label):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "env": dict(env), "timeout": timeout, "label": label})
        return self.result


@pytest.fixture()
def project(tmp_path, monkeypatch):
    d = tmp_path / "project"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


@pytest.fixture()
def fake_clis(daniel, tmp_path, monkeypatch):
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "auth.json").write_text("{}")
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)
    monkeypatch.setattr(daniel.shutil, "which", lambda name: f"/fake/bin/{name}")


# ---------------- Grok ----------------

def test_grok_default_has_no_bypass_flags(daniel, cfg, project, fake_clis, monkeypatch):
    cap = Capture(stdout=json.dumps({"text": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    assert daniel._call_grok_cli("hi", cfg) == "ok"
    cmd = cap.calls[0]["cmd"]
    for flag in BYPASS_FLAGS:
        assert flag not in cmd
    assert "off" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace"


def test_grok_default_config_value_is_sandboxed(daniel):
    assert daniel.DEFAULTS["grok_cli_permission_mode"] == "sandboxed-auto"
    assert daniel.DEFAULTS["antigravity_cli_permission_mode"] == "sandboxed-auto"


def test_grok_garbage_permission_mode_falls_back_to_sandbox(daniel, cfg, project, fake_clis, monkeypatch):
    cfg["grok_cli_permission_mode"] = "yolo-please"
    cap = Capture(stdout=json.dumps({"text": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_grok_cli("hi", cfg)
    assert "--always-approve" not in cap.calls[0]["cmd"]


def test_grok_opt_in_still_produces_bypass(daniel, cfg, project, fake_clis, monkeypatch):
    cfg["grok_cli_permission_mode"] = "full-host-unattended"
    cap = Capture(stdout=json.dumps({"text": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_grok_cli("hi", cfg)
    cmd = cap.calls[0]["cmd"]
    assert "--always-approve" in cmd
    assert "bypassPermissions" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "off"


@pytest.mark.parametrize("which", ["grok", "antigravity"])
def test_env_allowlist_excludes_secrets(daniel, cfg, project, fake_clis, monkeypatch, which):
    monkeypatch.setenv("FAKE_SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-nope")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    cap = Capture(stdout=json.dumps({"text": "ok", "status": "SUCCESS", "response": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    getattr(daniel, f"_call_{which}_cli")("hi", cfg)
    env = cap.calls[0]["env"]
    assert "FAKE_SECRET_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "hunter2" not in env.values()
    assert env["LC_ALL"] == "C.UTF-8"
    assert "PATH" in env and "HOME" in env


def test_grok_gets_xai_key_when_set(daniel, cfg, project, fake_clis, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    cap = Capture(stdout=json.dumps({"text": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_grok_cli("hi", cfg)
    assert cap.calls[0]["env"]["XAI_API_KEY"] == "xai-test"


@pytest.mark.parametrize("which", ["grok", "antigravity"])
def test_cwd_is_job_dir_not_home(daniel, cfg, project, fake_clis, monkeypatch, tmp_path, which):
    cap = Capture(stdout=json.dumps({"text": "ok", "status": "SUCCESS", "response": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    getattr(daniel, f"_call_{which}_cli")("hi", cfg)
    cwd = cap.calls[0]["cwd"]
    assert Path(cwd).resolve() == project.resolve()
    assert Path(cwd).resolve() != tmp_path.resolve()
    if which == "grok":
        cmd = cap.calls[0]["cmd"]
        assert cmd[cmd.index("--cwd") + 1] == cwd


def test_refuses_home_as_cwd(daniel, cfg, fake_clis, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # HOME itself
    cfg["tasks_root"] = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(daniel, "_run_cli", Capture())
    with pytest.raises(RuntimeError, match="HOME"):
        daniel._call_grok_cli("hi", cfg)


def test_cwd_outside_allowed_dirs_uses_tasks_root(daniel, cfg, fake_clis, monkeypatch, tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    elsewhere = tmp_path.parent / "elsewhere-not-allowed"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.chdir(elsewhere)
    cap = Capture(stdout=json.dumps({"text": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_grok_cli("hi", cfg)
    assert Path(cap.calls[0]["cwd"]).resolve() == tasks.resolve()


# ---------------- Antigravity ----------------

def test_antigravity_default_command(daniel, cfg, project, fake_clis, monkeypatch):
    cap = Capture(stdout=json.dumps({"status": "SUCCESS", "response": "ok\n"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    assert daniel._call_antigravity_cli("hello", cfg) == "ok"
    call = cap.calls[0]
    cmd = call["cmd"]
    assert cmd[0] == "agy" and cmd[1] == "--print" and cmd[2] == "hello"
    assert "--dangerously-skip-permissions" not in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--print-timeout") + 1] == "1800s"
    assert "--model" not in cmd  # empty model = agy default


def test_antigravity_opt_in_and_model(daniel, cfg, project, fake_clis, monkeypatch):
    cfg["antigravity_cli_permission_mode"] = "full-host-unattended"
    cfg["models"]["antigravity"] = "gemini-3.8-flash-high"
    cap = Capture(stdout=json.dumps({"status": "SUCCESS", "response": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_antigravity_cli("hello", cfg)
    cmd = cap.calls[0]["cmd"]
    assert "--dangerously-skip-permissions" in cmd
    assert "--sandbox" not in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high"


def test_antigravity_timeout_from_env(daniel, cfg, project, fake_clis, monkeypatch):
    monkeypatch.setenv("DANIEL_ANTIGRAVITY_TIMEOUT_SECS", "42")
    cap = Capture(stdout=json.dumps({"status": "SUCCESS", "response": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_antigravity_cli("hello", cfg)
    assert "42s" in cap.calls[0]["cmd"]
    assert cap.calls[0]["timeout"] == 72


def test_antigravity_print_timeout_is_an_error(daniel, cfg, project, fake_clis, monkeypatch):
    cap = Capture(stdout=json.dumps({"status": "SUCCESS", "response": ""}),
                  stderr="[agy] print timeout after 5s with turn in progress; returning partial output")
    monkeypatch.setattr(daniel, "_run_cli", cap)
    with pytest.raises(RuntimeError, match="timed out"):
        daniel._call_antigravity_cli("hello", cfg)


def test_antigravity_unavailable_without_signin(daniel, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(daniel.shutil, "which", lambda n: "/fake/agy")
    assert daniel._available("antigravity", cfg) is False
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)
    assert daniel._available("antigravity", cfg) is True


def test_run_cli_kills_process_group_on_timeout(daniel, tmp_path):
    marker = tmp_path / "child-survived"
    cmd = ["sh", "-c", f"(sleep 3; touch {marker}) & sleep 30"]
    start = time.time()
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        daniel._run_cli(cmd, str(tmp_path), {"PATH": os.environ["PATH"]}, 1, "x")
    assert time.time() - start < 10
    time.sleep(3.5)
    assert not marker.exists()  # grandchild was killed with the group


# ---------------- error classification ----------------

def _down(cfg, agent):
    ov = cfg["service_overrides"][agent]
    return ov["manual_disabled"], ov["disabled_until"]


def test_antigravity_auth_error_manual_down(daniel, cfg):
    note = daniel._apply_auto_downtime("antigravity", "antigravity CLI failed: Please sign in to continue", cfg)
    assert note and "/service up antigravity" in note
    assert _down(cfg, "antigravity") == (True, "")


@pytest.mark.parametrize("msg", ["HTTP 429 Too Many Requests", "RESOURCE_EXHAUSTED", "Quota exceeded for model", "rate limit hit"])
def test_antigravity_quota_timed_down(daniel, cfg, msg):
    note = daniel._apply_auto_downtime("antigravity", msg, cfg)
    assert note and "retry in 5min" in note
    manual, until = _down(cfg, "antigravity")
    assert manual is False and until


def test_antigravity_timeout_is_just_an_error(daniel, cfg):
    assert daniel._apply_auto_downtime("antigravity", "antigravity CLI timed out after 1800s", cfg) is None
    assert _down(cfg, "antigravity") == (False, "")


@pytest.mark.parametrize("msg", [
    'argon API HTTP 402: {"error": "payment required"}',
    "argon API HTTP 429: RESOURCE_EXHAUSTED prepayment credits depleted",
    "argon API HTTP 400: Your billing account has insufficient credit",
    "You exceeded your current quota",
])
def test_argon_billing_exhaustion_manual_down(daniel, cfg, msg):
    note = daniel._apply_auto_downtime("argon", msg, cfg)
    assert note and "/service up argon" in note
    assert _down(cfg, "argon") == (True, "")


def test_argon_timeout_not_downed(daniel, cfg):
    assert daniel._apply_auto_downtime("argon", "argon API timed out after 600s", cfg) is None


# ---------------- Argon ----------------

class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_argon_unavailable_without_key(daniel, cfg):
    assert daniel._available("argon", cfg) is False
    cfg["keys"]["gemini"] = "k"
    assert daniel._available("argon", cfg) is True


def test_argon_key_from_env(daniel, cfg, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "envkey")
    assert daniel._available("argon", cfg) is True


def test_argon_request_shape_and_parse(daniel, cfg, monkeypatch):
    cfg["keys"]["gemini"] = "secret-key"
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return FakeResp(json.dumps({"steps": [{"type": "model_output", "content": [{"type": "text", "text": "LGTM"}]}]}))

    monkeypatch.setattr(daniel.urllib.request, "urlopen", fake_urlopen)
    assert daniel._call_argon("review this", cfg) == "LGTM"
    req = seen["req"]
    assert req.full_url == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert json.loads(req.data) == {"model": "gemini-4-argon", "input": "review this"}
    assert req.get_header("X-goog-api-key") == "secret-key"
    assert "secret-key" not in req.full_url


def test_argon_http_402_is_single_attempt_and_classified(daniel, cfg, monkeypatch):
    cfg["keys"]["gemini"] = "k"
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, None)

    monkeypatch.setattr(daniel.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        daniel._call_argon("x", cfg)
    assert len(calls) == 1
    assert "402" in str(ei.value)
    daniel._apply_auto_downtime("argon", str(ei.value), cfg)
    assert _down(cfg, "argon")[0] is True


def test_chat_once_never_retries_argon_after_exhaustion(daniel, cfg, monkeypatch):
    cfg["keys"]["gemini"] = "k"
    cfg["chains"]["review"] = ["argon", "codex"]
    monkeypatch.setattr(daniel.shutil, "which", lambda n: f"/fake/{n}")
    monkeypatch.setattr(daniel, "_try_save_config", lambda c: (True, ""))
    calls = []

    def fake_call(agent, prompt, cfg_, role):
        calls.append(agent)
        if agent == "argon":
            raise RuntimeError("argon API HTTP 402: payment required")
        return "codex says ok"

    monkeypatch.setattr(daniel, "_call_agent_with_spinner", fake_call)
    monkeypatch.setattr(daniel, "_shared_context", lambda *a, **k: "")
    agent, out, failures = daniel._chat_once("review", "msg", cfg, [], None)
    assert (agent, out) == ("codex", "codex says ok")
    assert calls == ["argon", "codex"]
    # second review: argon is out of rotation, never called again
    calls.clear()
    daniel._chat_once("review", "msg", cfg, [], None)
    assert calls == ["codex"]
    assert daniel._service_status("argon", cfg).startswith("down")


# ---------------- chains / routing ----------------

def test_default_chains(daniel):
    ch = daniel.DEFAULTS["chains"]
    for role in ("implementation", "uidocs", "review"):
        assert "antigravity" in ch[role]
        assert ch[role][-1] == "grok" or role == "review"
        assert ch[role].index("antigravity") > ch[role].index("gemini")
        assert ch[role].index("antigravity") < ch[role].index("grok")
    assert ch["review"][-1] == "grok"
    assert ch["review"].index("antigravity") < ch["review"].index("argon") < ch["review"].index("grok")
    assert "antigravity" not in ch["orchestrator"] and "antigravity" not in ch["intel"]
    # owner's existing order is otherwise untouched
    assert [a for a in ch["implementation"] if a != "antigravity"] == ["codex", "claude", "gemini", "grok"]
    assert [a for a in ch["uidocs"] if a != "antigravity"] == ["gemini", "codex", "claude", "grok"]


def test_argon_only_in_review_chain(daniel):
    for role, chain in daniel.DEFAULTS["chains"].items():
        assert ("argon" in chain) == (role == "review")


def test_role_chain_filters_argon_outside_review(daniel, cfg, monkeypatch):
    cfg["keys"]["gemini"] = "k"
    cfg["chains"]["implementation"] = ["argon", "codex"]
    monkeypatch.setattr(daniel.shutil, "which", lambda n: f"/fake/{n}")
    assert "argon" not in daniel._role_chain("implementation", cfg)
    assert "argon" in daniel._role_chain("review", cfg)
    # last-resort fallback must not pull argon into other roles either
    cfg["chains"]["uidocs"] = []
    for a in ("claude", "codex", "gemini", "grok", "antigravity"):
        cfg["service_overrides"][a]["manual_disabled"] = True
    assert daniel._role_chain("uidocs", cfg) == []


def test_argon_not_in_review_when_no_key(daniel, cfg, monkeypatch):
    monkeypatch.setattr(daniel.shutil, "which", lambda n: f"/fake/{n}")
    assert "argon" not in daniel._role_chain("review", cfg)


def test_at_addressing(daniel):
    assert daniel._route_message("@antigravity do x") == ("implementation", "do x", "antigravity")
    assert daniel._route_message("@argon do x")[2] is None  # paid reviewer: chain only


def test_config_roundtrip_has_new_agents(daniel):
    cfg = daniel._load_config()
    for a in ("antigravity", "argon"):
        assert a in cfg["service_overrides"]
        assert a in daniel.AGENT_COLORS
        assert daniel._agent_prompt_identity(a).startswith("You are")
    assert cfg["models"]["argon"] == "gemini-4-argon"


def test_permissions_command_accepts_antigravity(daniel, cfg, monkeypatch):
    monkeypatch.setattr(daniel, "_try_save_config", lambda c: (True, ""))
    ok, _ = daniel._set_cli_permission_mode(cfg, "antigravity", "full-host-unattended")
    assert ok and cfg["antigravity_cli_permission_mode"] == "full-host-unattended"
    ok, _ = daniel._set_cli_permission_mode(cfg, "claude", "sandboxed-auto")
    assert not ok


def test_wizard_invalid_permission_falls_back_to_sandbox(daniel, cfg, monkeypatch, tmp_path):
    answers = {}

    def fake_prompt(label, default=""):
        if "Grok CLI permission" in label or "Antigravity (agy) CLI permission" in label:
            return "nonsense"
        return default

    monkeypatch.setattr(daniel, "_prompt", fake_prompt)
    monkeypatch.setattr(daniel, "getpass", lambda *a, **k: "")
    monkeypatch.setattr(daniel.shutil, "which", lambda n: f"/fake/{n}")
    monkeypatch.setattr(daniel, "_run_init_shared", lambda *a, **k: None)
    monkeypatch.setattr(daniel, "_write_tasks_env", lambda c: None)
    monkeypatch.setattr(daniel, "_save_config", lambda c: None)
    out = daniel.setup_wizard(cfg, force=False)
    assert out["grok_cli_permission_mode"] == "sandboxed-auto"
    assert out["antigravity_cli_permission_mode"] == "sandboxed-auto"


def test_legacy_grok_build_model_id_is_not_passed(daniel, cfg, project, fake_clis, monkeypatch):
    cfg["models"]["grok"] = "grok-build"  # invalid id in grok 1.0.50; owner configs still carry it
    cap = Capture(stdout=json.dumps({"text": "ok"}))
    monkeypatch.setattr(daniel, "_run_cli", cap)
    daniel._call_grok_cli("hi", cfg)
    assert "--model" not in cap.calls[0]["cmd"]
    cfg["models"]["grok"] = "grok-4.7"
    daniel._call_grok_cli("hi", cfg)
    cmd = cap.calls[1]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "grok-4.7"
