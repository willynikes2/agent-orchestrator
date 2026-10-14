#!/usr/bin/env python3
"""Multi-agent terminal orchestrator with CLI wrapping and next-man-up failover."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from getpass import getpass
from pathlib import Path
from datetime import datetime, timedelta, timezone

AGENTS = ("claude", "codex", "gemini", "grok", "antigravity", "argon")
# Agents that may only ever appear in the given roles' chains (argon is a paid API reviewer).
ROLE_RESTRICTED_AGENTS = {"argon": ("review",)}
# Agents that cost real money per call: never smoke-tested, never addressed directly with @.
PAID_API_AGENTS = ("argon",)
ARGON_MODEL = "gemini-4-argon"
ARGON_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
ARGON_TIMEOUT_SECS = 600
ANTIGRAVITY_CLI_TIMEOUT_SECS = 1800
GEMINI_CLI_TIMEOUT_SECS = 300  # 5 minutes (was 120 — too short for complex prompts)
GROK_CLI_TIMEOUT_SECS = 1800
GEMINI_KNOWN_MODELS = ("gemini-2.5-pro", "gemini-2.5-flash")
DEFAULTS = {
    "name": "agent",
    "tasks_root": str(Path.home() / "tasks"),
    "kb_port": "3838",
    "claude_mode": "cli",
    "codex_mode": "cli",
    "gemini_mode": "cli",
    "grok_mode": "cli",
    "codex_cli_permission_mode": "full-host-unattended",
    "gemini_cli_permission_mode": "full-host-unattended",
    # Grok and Antigravity default to a sandboxed mode. full-host-unattended is an explicit opt-in.
    "grok_cli_permission_mode": "sandboxed-auto",
    "antigravity_mode": "cli",
    "antigravity_cli_permission_mode": "sandboxed-auto",
    "models": {
        "claude": "claude-sonnet-4-5",
        "codex": "gpt-5",
        "codex_orchestrator": "gpt-5",
        "gemini": "gemini-2.5-pro",
        "grok": "grok-4.7",
        "antigravity": "",  # empty = agy's own default model
        "argon": ARGON_MODEL,
    },
    "chains": {
        "orchestrator": ["claude", "codex", "gemini", "grok"],
        "implementation": ["codex", "claude", "gemini", "antigravity", "grok"],
        "uidocs": ["gemini", "codex", "claude", "antigravity", "grok"],
        "review": ["claude", "codex", "gemini", "antigravity", "argon", "grok"],
        "intel": ["grok", "gemini", "claude", "codex"],
    },
    "keys": {
        "openai": "",
        "anthropic": "",
        "gemini": "",
        "xai": "",
    },
    "service_overrides": {
        "claude": {"manual_disabled": False, "disabled_until": ""},
        "codex": {"manual_disabled": False, "disabled_until": ""},
        "gemini": {"manual_disabled": False, "disabled_until": ""},
        "grok": {"manual_disabled": False, "disabled_until": ""},
        "antigravity": {"manual_disabled": False, "disabled_until": ""},
        "argon": {"manual_disabled": False, "disabled_until": ""},
    },
    "allowed_dirs": [str(Path.home())],
}

CONFIG_DIR = Path.home() / ".config" / "agent-orchestrator"
CONFIG_PATH = CONFIG_DIR / "config.json"
# Backward compat: check old location
_OLD_CONFIG = Path.home() / ".config" / "daniel" / "config.json"

# --- Terminal colors (disabled if not a TTY) ---
_COLORS = sys.stdout.isatty()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLORS else text

AGENT_COLORS = {"claude": "35", "codex": "32", "gemini": "34", "grok": "36", "antigravity": "33", "argon": "31"}  # magenta, green, blue, cyan, yellow, red


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_config() -> dict:
    # Check old config location for backward compat
    path = CONFIG_PATH
    if not path.exists() and _OLD_CONFIG.exists():
        path = _OLD_CONFIG
    if not path.exists():
        return json.loads(json.dumps(DEFAULTS))
    raw = json.loads(_read(path))
    cfg = json.loads(json.dumps(DEFAULTS))
    for top in (
        "name",
        "tasks_root",
        "kb_port",
        "claude_mode",
        "codex_mode",
        "gemini_mode",
        "grok_mode",
        "codex_cli_permission_mode",
        "gemini_cli_permission_mode",
        "grok_cli_permission_mode",
        "antigravity_mode",
        "antigravity_cli_permission_mode",
        "models",
        "chains",
        "keys",
        "service_overrides",
        "allowed_dirs",
    ):
        if top not in raw:
            continue
        if isinstance(cfg[top], dict):
            cfg[top].update(raw[top])
        else:
            cfg[top] = raw[top]
    for agent in AGENTS:
        cfg["service_overrides"].setdefault(agent, {"manual_disabled": False, "disabled_until": ""})
        ov = cfg["service_overrides"][agent]
        if not isinstance(ov, dict):
            cfg["service_overrides"][agent] = {"manual_disabled": False, "disabled_until": ""}
            continue
        ov.setdefault("manual_disabled", False)
        ov.setdefault("disabled_until", "")
    if not isinstance(cfg.get("allowed_dirs"), list):
        cfg["allowed_dirs"] = [str(Path.home())]
    cfg["allowed_dirs"] = [str(Path(p).expanduser()) for p in cfg["allowed_dirs"] if str(p).strip()]
    return cfg


def _save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def _try_save_config(cfg: dict) -> tuple[bool, str]:
    try:
        _save_config(cfg)
        return True, ""
    except Exception as exc:
        return False, str(exc)


CLI_PERMISSION_MODES = ("full-host-unattended", "sandboxed-auto")
SANDBOXED_MODE = "sandboxed-auto"
FULL_HOST_MODE = "full-host-unattended"
# Agents whose default permission mode is the sandbox (full-host is opt-in only).
SANDBOX_DEFAULT_AGENTS = ("grok", "antigravity")


def _safe_permission_mode(value: object) -> str:
    """Anything other than the exact opt-in string falls back to the sandboxed mode."""
    return FULL_HOST_MODE if str(value or "").strip().lower() == FULL_HOST_MODE else SANDBOXED_MODE


def _cli_permissions_payload(cfg: dict) -> dict:
    return {
        "codex": {
            "mode": cfg.get("codex_mode", "api"),
            "permission_mode": cfg.get("codex_cli_permission_mode", "full-host-unattended"),
            "model": cfg["models"]["codex"],
            "orchestrator_model": cfg["models"]["codex_orchestrator"],
        },
        "gemini": {
            "mode": cfg.get("gemini_mode", "api"),
            "permission_mode": cfg.get("gemini_cli_permission_mode", "full-host-unattended"),
            "model": cfg["models"]["gemini"],
        },
        "grok": {
            "mode": cfg.get("grok_mode", "cli"),
            "permission_mode": _safe_permission_mode(cfg.get("grok_cli_permission_mode")),
            "model": cfg["models"]["grok"],
        },
        "antigravity": {
            "mode": cfg.get("antigravity_mode", "cli"),
            "permission_mode": _safe_permission_mode(cfg.get("antigravity_cli_permission_mode")),
            "model": cfg["models"].get("antigravity", ""),
        },
    }


def _set_cli_permission_mode(cfg: dict, agent: str, permission_mode: str) -> tuple[bool, str]:
    if agent not in ("codex", "gemini", "grok", "antigravity"):
        return False, "Only codex, gemini, grok, and antigravity support runtime CLI permission mode changes."
    if permission_mode not in CLI_PERMISSION_MODES:
        return False, f"Permission mode must be one of: {', '.join(CLI_PERMISSION_MODES)}"
    key = f"{agent}_cli_permission_mode"
    cfg[key] = permission_mode
    ok, err = _try_save_config(cfg)
    if not ok:
        return False, f"warning: updated in memory but could not persist config: {err}"
    return True, f"{agent} permission mode set to {permission_mode}"


def _parse_chain(raw: str, default: list[str]) -> list[str]:
    raw = raw.strip()
    if not raw:
        return list(default)
    out: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if part in AGENTS and part not in out:
            out.append(part)
    return out or list(default)


def _agent_prompt_identity(agent: str) -> str:
    if agent == "codex":
        return "You are Codex, the implementation-heavy engineering agent."
    if agent == "gemini":
        return "You are Gemini, the UI/docs and second-opinion agent."
    if agent == "claude":
        return "You are Claude, the orchestrator/review agent."
    if agent == "grok":
        return "You are Grok, the X/Twitter intelligence and external-signal analysis agent."
    if agent == "antigravity":
        return "You are Antigravity, Google's agentic coding assistant (agy), used for implementation, UI/docs and review."
    if agent == "argon":
        return "You are Argon, Google's Gemini 4 Argon model, acting as an independent code reviewer."
    return f"You are {agent.title()}, a pragmatic software assistant."


def _default_role_for_direct_agent(agent: str) -> str:
    if agent == "codex":
        return "implementation"
    if agent == "gemini":
        return "uidocs"
    if agent == "claude":
        return "review"
    if agent == "grok":
        return "intel"
    if agent == "antigravity":
        return "implementation"
    if agent == "argon":
        return "review"
    return "orchestrator"


def _key_for(agent: str, cfg: dict) -> str:
    mapping = {
        "codex": cfg["keys"]["openai"] if cfg.get("codex_mode", "api") == "api" else "cli",
        "claude": cfg["keys"]["anthropic"] if cfg.get("claude_mode", "cli") == "api" else "cli",
        "gemini": cfg["keys"]["gemini"] if cfg.get("gemini_mode", "api") == "api" else "cli",
        "grok": cfg["keys"]["xai"] if cfg.get("grok_mode", "cli") == "api" else "cli",
        "antigravity": "cli",
        "argon": _argon_key(cfg),
    }
    return mapping[agent].strip()


def _argon_key(cfg: dict) -> str:
    """Gemini API key for Argon (config keys, then GEMINI_API_KEY). Never printed."""
    return (str(cfg.get("keys", {}).get("gemini", "")).strip() or os.environ.get("GEMINI_API_KEY", "").strip())


def _antigravity_auth_configured() -> bool:
    """agy has no non-interactive auth probe; check that its sign-in state directory exists."""
    return (Path.home() / ".gemini" / "antigravity-cli").is_dir()


def _grok_auth_configured(cfg: dict) -> bool:
    return bool(
        str(cfg.get("keys", {}).get("xai", "")).strip()
        or os.environ.get("XAI_API_KEY", "").strip()
        or (Path.home() / ".grok" / "auth.json").exists()
    )


def _available(agent: str, cfg: dict) -> bool:
    if agent == "codex":
        mode = cfg.get("codex_mode", "api")
        if mode == "api":
            return bool(cfg["keys"]["openai"].strip())
        if mode == "cli":
            return shutil.which("codex") is not None
        return False
    if agent == "claude":
        mode = cfg.get("claude_mode", "cli")
        if mode == "api":
            return bool(cfg["keys"]["anthropic"].strip())
        if mode == "cli":
            return shutil.which("claude") is not None
        return False
    if agent == "gemini":
        mode = cfg.get("gemini_mode", "api")
        if mode == "api":
            return bool(cfg["keys"]["gemini"].strip())
        if mode == "cli":
            return shutil.which("gemini") is not None
        return False
    if agent == "grok":
        mode = cfg.get("grok_mode", "cli")
        if mode == "cli":
            return shutil.which("grok") is not None and _grok_auth_configured(cfg)
        return False
    if agent == "antigravity":
        if cfg.get("antigravity_mode", "cli") == "cli":
            return shutil.which("agy") is not None and _antigravity_auth_configured()
        return False
    if agent == "argon":
        return bool(_argon_key(cfg))
    return bool(_key_for(agent, cfg))


def _parse_iso_utc(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _service_disabled_reason(agent: str, cfg: dict, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    override = cfg.get("service_overrides", {}).get(agent, {})
    if override.get("manual_disabled", False):
        return "manually disabled"
    until = _parse_iso_utc(str(override.get("disabled_until", "")))
    if until is None:
        return None
    if now < until:
        return f"disabled until { _format_iso_utc(until) }"
    override["disabled_until"] = ""
    return None


def _service_status(agent: str, cfg: dict) -> str:
    if agent == "grok" and shutil.which("grok") is not None and not _grok_auth_configured(cfg):
        return "unavailable (missing auth: run `grok login --device-auth` or set XAI_API_KEY)"
    if agent == "antigravity" and shutil.which("agy") is not None and not _antigravity_auth_configured():
        return "unavailable (not signed in: run `agy` once interactively to sign in)"
    if agent == "argon" and not _argon_key(cfg):
        return "unavailable (no Gemini API key: set keys.gemini or GEMINI_API_KEY)"
    if not _available(agent, cfg):
        return "unavailable (missing key/cli)"
    reason = _service_disabled_reason(agent, cfg)
    if reason:
        return f"down ({reason})"
    return "up"


def _parse_claude_reset_utc(error_text: str) -> datetime | None:
    m = re.search(r"resets\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{1,2}(?::\d{2})?(?:am|pm)\s+\(UTC\))", error_text, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1)
    now = datetime.now(timezone.utc)
    for fmt in ("%b %d, %I:%M%p (UTC)", "%b %d, %I%p (UTC)"):
        try:
            parsed = datetime.strptime(raw, fmt)
            candidate = parsed.replace(year=now.year, tzinfo=timezone.utc)
            if candidate < now - timedelta(hours=1):
                candidate = candidate.replace(year=now.year + 1)
            return candidate
        except ValueError:
            continue
    return None


def _apply_auto_downtime(agent: str, error_text: str, cfg: dict) -> str | None:
    text = (error_text or "").strip()
    if not text:
        return None
    override = cfg["service_overrides"][agent]

    if agent == "claude" and "out of extra usage" in text.lower():
        reset_at = _parse_claude_reset_utc(text)
        if reset_at is not None:
            override["manual_disabled"] = False
            override["disabled_until"] = _format_iso_utc(reset_at)
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} until {override['disabled_until']}{suffix}"

    if agent == "gemini":
        lower = text.lower()
        if "modelnot found" in lower or "requested entity was not found" in lower or "model not found" in lower:
            override["manual_disabled"] = True
            override["disabled_until"] = ""
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} manual (model not found). Fix model then run /service up {agent}.{suffix}"
        if "resource_exhausted" in lower or "quota" in lower or "rate limit" in lower:
            override["disabled_until"] = _format_iso_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} rate limited, retry in 5min{suffix}"
        if "permission_denied" in lower or "api_key_invalid" in lower or "unauthorized" in lower:
            override["manual_disabled"] = True
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} auth failed. Check API key then /service up {agent}.{suffix}"

    if agent == "codex":
        lower = text.lower()
        if "rate limit" in lower or "429" in lower or "resource exhausted" in lower:
            override["disabled_until"] = _format_iso_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} rate limited, retry in 5min{suffix}"
        if "authentication" in lower or "401" in lower or "invalid api key" in lower:
            override["manual_disabled"] = True
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} auth failed. Check API key then /service up {agent}.{suffix}"

    if agent == "grok":
        lower = text.lower()
        if "rate limit" in lower or "429" in lower or "resource exhausted" in lower or "quota" in lower:
            override["disabled_until"] = _format_iso_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} rate limited, retry in 5min{suffix}"
        if (
            "not authenticated" in lower
            or "unauthorized" in lower
            or "401" in lower
            or "xai_api_key" in lower
            or "authentication" in lower
        ):
            override["manual_disabled"] = True
            override["disabled_until"] = ""
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} auth failed. Run `grok login --device-auth` or set XAI_API_KEY, then /service up {agent}.{suffix}"

    if agent == "antigravity":
        lower = text.lower()
        # Timeouts are plain errors: no downtime, the chain just moves on.
        if "timed out" in lower:
            return None
        if "rate limit" in lower or re.search(r"\b429\b", lower) or "resource exhausted" in lower or "resource_exhausted" in lower or "quota" in lower:
            override["disabled_until"] = _format_iso_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} rate limited, retry in 5min{suffix}"
        if (
            "sign in" in lower
            or "sign-in" in lower
            or "signed in" in lower
            or "not authenticated" in lower
            or "unauthenticated" in lower
            or "unauthorized" in lower
            or re.search(r"\b401\b", lower)
            or "authentication" in lower
            or "log in" in lower
            or "login" in lower
        ):
            override["manual_disabled"] = True
            override["disabled_until"] = ""
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} auth failed. Sign in by running `agy` interactively, then /service up {agent}.{suffix}"

    if agent == "argon":
        lower = text.lower()
        if "timed out" in lower:
            return None
        # Out of credit / billing / quota: out of rotation until the operator says otherwise.
        # Argon is prepaid with no card, so we never retry and never top up anything.
        if (
            re.search(r"\b402\b", lower)
            or re.search(r"\b429\b", lower)
            or "payment required" in lower
            or "billing" in lower
            or "insufficient" in lower
            or "credit" in lower
            or "prepay" in lower
            or "quota" in lower
            or "resource exhausted" in lower
            or "resource_exhausted" in lower
            or "rate limit" in lower
        ):
            override["manual_disabled"] = True
            override["disabled_until"] = ""
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} out of credit/quota. Out of rotation until /service up {agent}; nothing is retried or topped up.{suffix}"
        if (
            re.search(r"\b40[13]\b", lower)
            or "api key" in lower
            or "api_key_invalid" in lower
            or "permission_denied" in lower
            or "unauthorized" in lower
        ):
            override["manual_disabled"] = True
            override["disabled_until"] = ""
            ok, err = _try_save_config(cfg)
            suffix = "" if ok else f" (warning: could not persist config: {err})"
            return f"auto-down: {agent} auth failed. Check the Gemini API key then /service up {agent}.{suffix}"

    return None


def _agent_allowed_in_role(agent: str, role: str) -> bool:
    allowed_roles = ROLE_RESTRICTED_AGENTS.get(agent)
    return allowed_roles is None or role in allowed_roles


def _role_chain(role: str, cfg: dict) -> list[str]:
    def ok(a: str) -> bool:
        return _agent_allowed_in_role(a, role) and _available(a, cfg) and not _service_disabled_reason(a, cfg)

    configured = list(cfg["chains"].get(role, []))
    filtered = [a for a in configured if ok(a)]
    if filtered:
        return filtered
    defaults = [a for a in DEFAULTS["chains"][role] if ok(a)]
    if defaults:
        return defaults
    return [a for a in AGENTS if ok(a)]


def _extract_openai_text(resp: object) -> str:
    output_text = getattr(resp, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    try:
        output = getattr(resp, "output", None) or []
        chunks: list[str] = []
        for item in output:
            content = getattr(item, "content", None) or []
            for c in content:
                text = getattr(c, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks).strip()
    except Exception:
        return str(resp)


def _call_codex_api(prompt: str, cfg: dict, role: str) -> str:
    from openai import OpenAI  # type: ignore

    model = cfg["models"]["codex_orchestrator"] if role == "orchestrator" else cfg["models"]["codex"]
    client = OpenAI(api_key=cfg["keys"]["openai"])
    resp = client.responses.create(model=model, input=prompt)
    out = _extract_openai_text(resp)
    return out or "<empty response>"


def _codex_cli_model(cfg: dict, role: str) -> str:
    return cfg["models"]["codex_orchestrator"] if role == "orchestrator" else cfg["models"]["codex"]


def _call_codex_cli(prompt: str, cfg: dict, role: str) -> str:
    if shutil.which("codex") is None:
        raise RuntimeError("Codex CLI not found on PATH")
    cli_cwd = _cli_workdir(cfg)
    allowed = _allowed_dirs(cfg)
    with tempfile.NamedTemporaryFile(prefix="daniel-codex-", suffix=".txt", delete=False) as tmp:
        out_path = tmp.name
    permission_mode = cfg.get("codex_cli_permission_mode", "full-host-unattended")
    cmd = [
        "codex",
        "exec",
        "--model",
        _codex_cli_model(cfg, role),
        "--cd",
        cli_cwd or str(Path.home()),
        "--skip-git-repo-check",
        "--color",
        "never",
        "-o",
        out_path,
    ]
    if permission_mode == "full-host-unattended":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--full-auto", "--sandbox", "workspace-write"])
    for d in allowed:
        cmd.extend(["--add-dir", d])
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        cwd=cli_cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=1800,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit {proc.returncode}"
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(f"codex CLI failed: {err}")
    out = _read(Path(out_path)).strip()
    try:
        os.remove(out_path)
    except OSError:
        pass
    return out or "<empty response>"


def _call_codex(prompt: str, cfg: dict, role: str) -> str:
    mode = cfg.get("codex_mode", "api")
    if mode == "api":
        return _call_codex_api(prompt, cfg, role)
    if mode == "cli":
        return _call_codex_cli(prompt, cfg, role)
    raise RuntimeError(f"Unsupported codex_mode: {mode}")


def _call_claude_api(prompt: str, cfg: dict) -> str:
    from anthropic import Anthropic  # type: ignore

    client = Anthropic(api_key=cfg["keys"]["anthropic"])
    resp = client.messages.create(
        model=cfg["models"]["claude"],
        max_tokens=2200,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    chunks: list[str] = []
    for block in getattr(resp, "content", []):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks).strip() or "<empty response>"


def _call_claude_cli(prompt: str, cfg: dict) -> str:
    if shutil.which("claude") is None:
        raise RuntimeError("Claude CLI not found on PATH")
    cli_cwd = _cli_workdir(cfg)
    allowed = _allowed_dirs(cfg)
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--allow-dangerously-skip-permissions",
        "--dangerously-skip-permissions",
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        *(allowed or [cli_cwd or str(Path.home())]),
        "--model",
        cfg["models"]["claude"],
        prompt,
    ]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    proc = subprocess.run(
        cmd,
        cwd=cli_cwd,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"claude CLI failed: {err}")
    out = (proc.stdout or "").strip()
    return out or "<empty response>"


def _call_claude(prompt: str, cfg: dict) -> str:
    mode = cfg.get("claude_mode", "cli")
    if mode == "api":
        return _call_claude_api(prompt, cfg)
    if mode == "cli":
        return _call_claude_cli(prompt, cfg)
    raise RuntimeError(f"Unsupported claude_mode: {mode}")


def _call_gemini_api(prompt: str, cfg: dict) -> str:
    from google import genai  # type: ignore

    client = genai.Client(api_key=cfg["keys"]["gemini"])
    last_error: Exception | None = None
    for model in _gemini_model_candidates(cfg):
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
        except Exception as exc:
            if not _is_gemini_model_not_found_error(str(exc)):
                raise
            last_error = exc
            continue
        _persist_gemini_model_recovery(cfg, model)
        text = getattr(resp, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        return str(resp)
    if last_error is not None:
        raise RuntimeError(str(last_error))
    raise RuntimeError("gemini API failed without returning output")


def _call_gemini_cli(prompt: str, cfg: dict) -> str:
    if shutil.which("gemini") is None:
        raise RuntimeError("Gemini CLI not found on PATH")
    cli_cwd = _cli_workdir(cfg)
    permission_mode = cfg.get("gemini_cli_permission_mode", "full-host-unattended")
    last_error: str | None = None
    for model in _gemini_model_candidates(cfg):
        # Pass prompt via -p flag (Gemini CLI headless mode)
        # Force text output for reliability — JSON parsing was fragile.
        cmd = [
            "gemini",
            "-p", prompt,
            "--model", model,
            "--output-format", "text",
        ]
        if permission_mode == "full-host-unattended":
            cmd.extend(["--approval-mode", "yolo", "--sandbox=false"])
        else:
            cmd.extend(["--approval-mode", "yolo"])
        try:
            proc = subprocess.run(
                cmd,
                cwd=cli_cwd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=GEMINI_CLI_TIMEOUT_SECS,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gemini CLI timed out after {GEMINI_CLI_TIMEOUT_SECS}s")
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit {proc.returncode}"
            if _is_gemini_model_not_found_error(err):
                last_error = err
                continue
            raise RuntimeError(f"gemini CLI failed: {err}")
        raw = (proc.stdout or "").strip()
        if not raw:
            # Check stderr for clues
            stderr = (proc.stderr or "").strip()
            if stderr:
                raise RuntimeError(f"gemini CLI empty output, stderr: {stderr}")
            _persist_gemini_model_recovery(cfg, model)
            return "<empty response>"
        _persist_gemini_model_recovery(cfg, model)
        # Try to extract text from JSON if Gemini returns structured output
        try:
            parsed = json.loads(raw)
            # Gemini CLI JSON format: candidates[0].content.parts[0].text
            if isinstance(parsed, dict):
                candidates = parsed.get("candidates", [])
                if candidates and isinstance(candidates, list):
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and isinstance(parts, list):
                        text = parts[0].get("text", "")
                        if text.strip():
                            return text.strip()
                # Fallback: try flat keys
                for key in ("text", "content", "response"):
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            pass
        return raw
    if last_error is not None:
        raise RuntimeError(f"gemini CLI failed: {last_error}")
    raise RuntimeError("gemini CLI failed without returning output")


def _is_gemini_model_not_found_error(text: str) -> bool:
    lower = (text or "").lower()
    return (
        "modelnot found" in lower
        or "requested entity was not found" in lower
        or "model not found" in lower
    )


def _gemini_model_candidates(cfg: dict) -> list[str]:
    candidates: list[str] = []
    configured = str(cfg.get("models", {}).get("gemini", "")).strip()
    for model in (configured, DEFAULTS["models"]["gemini"], *GEMINI_KNOWN_MODELS):
        cleaned = str(model or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


def _persist_gemini_model_recovery(cfg: dict, model: str) -> None:
    cleaned = str(model or "").strip()
    if not cleaned or cfg["models"].get("gemini") == cleaned:
        return
    cfg["models"]["gemini"] = cleaned
    _try_save_config(cfg)


def _call_gemini(prompt: str, cfg: dict) -> str:
    mode = cfg.get("gemini_mode", "api")
    if mode == "api":
        return _call_gemini_api(prompt, cfg)
    if mode == "cli":
        return _call_gemini_cli(prompt, cfg)
    raise RuntimeError(f"Unsupported gemini_mode: {mode}")


# Environment variables an unattended agent CLI may inherit. Everything else (cloud keys,
# tokens, DB URLs, ...) is deliberately NOT passed to the child process.
CLI_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME")
CLI_ENV_ALLOW_PREFIXES = ("LC_",)


def _minimal_cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in CLI_ENV_ALLOWLIST or key.startswith(CLI_ENV_ALLOW_PREFIXES):
            env[key] = value
    for key, value in (extra or {}).items():
        if value:
            env[key] = value
    return env


def _job_workdir(cfg: dict, agent: str) -> str:
    """Directory an unattended CLI agent works in: where Daniel was started if approved, else the
    configured task dir. Never $HOME itself."""
    home = str(Path.home().resolve())
    candidates: list[str] = []
    try:
        here = Path.cwd().resolve()
        if _is_allowed_dir(here, cfg):
            candidates.append(str(here))
    except OSError:
        pass
    fallback = _cli_workdir(cfg)
    if fallback:
        candidates.append(fallback)
    for cand in candidates:
        if str(Path(cand).resolve()) != home:
            return cand
    raise RuntimeError(
        f"{agent}: refusing to run with $HOME as its working directory. "
        "Start Daniel inside a project directory (add it with /allow-dir add <path>)."
    )


def _cli_timeout_secs(cfg: dict, name: str, default: int) -> int:
    raw = os.environ.get(f"DANIEL_{name.upper()}_TIMEOUT_SECS") or cfg.get(f"{name}_timeout_secs")
    try:
        value = int(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _run_cli(cmd: list[str], cwd: str, env: dict[str, str], timeout: int, label: str) -> subprocess.CompletedProcess:
    """Run an agent CLI in its own process group so timeouts and Ctrl-C kill the whole tree."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def _kill_group() -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise RuntimeError(f"{label} CLI timed out after {timeout}s")
    except BaseException:
        _kill_group()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _grok_model(cfg: dict) -> str:
    """Configured Grok model; "" means grok's own default. "grok-build" was an invalid id in an
    earlier default (grok 1.0.50 rejects it), so it is treated as unset."""
    model = str(cfg.get("models", {}).get("grok", "")).strip()
    return "" if model == "grok-build" else model


def _grok_cli_command(prompt_path: str, cli_cwd: str, model: str, permission_mode: str) -> list[str]:
    cmd = ["grok", "--prompt-file", prompt_path, "--cwd", cli_cwd]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["--output-format", "json", "--no-plan"])
    if _safe_permission_mode(permission_mode) == FULL_HOST_MODE:
        # Explicit opt-in only. Use inside a container/VM.
        cmd.extend(["--always-approve", "--permission-mode", "bypassPermissions", "--sandbox", "off"])
    else:
        # Grok's own workspace sandbox; edits inside the workspace are accepted, nothing is bypassed.
        cmd.extend(["--sandbox", "workspace", "--permission-mode", "acceptEdits"])
    return cmd


def _call_grok_cli(prompt: str, cfg: dict) -> str:
    if shutil.which("grok") is None:
        raise RuntimeError("Grok CLI not found on PATH")
    if not _grok_auth_configured(cfg):
        raise RuntimeError("Grok CLI not authenticated. Run `grok login --device-auth` or set XAI_API_KEY.")
    cli_cwd = _job_workdir(cfg, "grok")
    model = _grok_model(cfg)
    with tempfile.NamedTemporaryFile(prefix="daniel-grok-", suffix=".txt", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(prompt)
        prompt_path = tmp.name
    cmd = _grok_cli_command(prompt_path, cli_cwd, model, cfg.get("grok_cli_permission_mode"))
    xai_key = os.environ.get("XAI_API_KEY", "").strip() or str(cfg.get("keys", {}).get("xai", "")).strip()
    env = _minimal_cli_env({"XAI_API_KEY": xai_key})
    timeout = _cli_timeout_secs(cfg, "grok", GROK_CLI_TIMEOUT_SECS)
    try:
        proc = _run_cli(cmd, cli_cwd, env, timeout, "grok")
    finally:
        try:
            os.remove(prompt_path)
        except OSError:
            pass
    raw_stdout = (proc.stdout or "").strip()
    raw_stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        err = raw_stderr or raw_stdout or f"exit {proc.returncode}"
        raise RuntimeError(f"grok CLI failed: {err}")
    if not raw_stdout:
        if raw_stderr:
            raise RuntimeError(f"grok CLI empty output, stderr: {raw_stderr}")
        return "<empty response>"
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return raw_stdout
    if isinstance(parsed, dict):
        if parsed.get("type") == "error":
            raise RuntimeError(f"grok CLI failed: {parsed.get('message', raw_stdout)}")
        text = parsed.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return raw_stdout


def _call_grok(prompt: str, cfg: dict) -> str:
    mode = cfg.get("grok_mode", "cli")
    if mode == "cli":
        return _call_grok_cli(prompt, cfg)
    raise RuntimeError(f"Unsupported grok_mode: {mode}")


def _antigravity_cli_command(prompt: str, model: str, permission_mode: str, timeout: int) -> list[str]:
    cmd = [
        "agy",
        "--print",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout}s",
    ]
    if model:
        cmd.extend(["--model", model])
    if _safe_permission_mode(permission_mode) == FULL_HOST_MODE:
        # Explicit opt-in only. Use inside a container/VM.
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd.extend(["--sandbox", "--mode", "accept-edits"])
    return cmd


def _call_antigravity_cli(prompt: str, cfg: dict) -> str:
    if shutil.which("agy") is None:
        raise RuntimeError("Antigravity CLI (agy) not found on PATH")
    if not _antigravity_auth_configured():
        raise RuntimeError("Antigravity CLI not signed in. Run `agy` once interactively to sign in.")
    cli_cwd = _job_workdir(cfg, "antigravity")
    timeout = _cli_timeout_secs(cfg, "antigravity", ANTIGRAVITY_CLI_TIMEOUT_SECS)
    model = str(cfg.get("models", {}).get("antigravity", "")).strip()
    cmd = _antigravity_cli_command(prompt, model, cfg.get("antigravity_cli_permission_mode"), timeout)
    # agy enforces --print-timeout itself; the outer limit only catches a hung process.
    proc = _run_cli(cmd, cli_cwd, _minimal_cli_env(), timeout + 30, "antigravity")
    raw_stdout = (proc.stdout or "").strip()
    raw_stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        err = raw_stderr or raw_stdout or f"exit {proc.returncode}"
        raise RuntimeError(f"antigravity CLI failed: {err}")
    if "print timeout" in raw_stderr.lower():
        raise RuntimeError(f"antigravity CLI timed out after {timeout}s")
    if not raw_stdout:
        if raw_stderr:
            raise RuntimeError(f"antigravity CLI empty output, stderr: {raw_stderr}")
        return "<empty response>"
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return raw_stdout
    if isinstance(parsed, dict):
        status = str(parsed.get("status", "SUCCESS")).upper()
        response = parsed.get("response")
        if status not in ("SUCCESS", "OK", "COMPLETED"):
            detail = (response if isinstance(response, str) and response.strip() else raw_stderr) or raw_stdout
            raise RuntimeError(f"antigravity CLI failed ({status}): {detail}")
        if isinstance(response, str) and response.strip():
            return response.strip()
        return "<empty response>"
    return raw_stdout


def _call_antigravity(prompt: str, cfg: dict) -> str:
    mode = cfg.get("antigravity_mode", "cli")
    if mode == "cli":
        return _call_antigravity_cli(prompt, cfg)
    raise RuntimeError(f"Unsupported antigravity_mode: {mode}")


def _argon_response_text(parsed: object) -> str:
    if not isinstance(parsed, dict):
        return ""
    chunks: list[str] = []
    for step in parsed.get("steps") or []:
        if isinstance(step, dict) and step.get("type") == "model_output":
            for item in step.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
    if chunks:
        return "".join(chunks).strip()
    text = parsed.get("output_text")
    return text.strip() if isinstance(text, str) else ""


def _call_argon(prompt: str, cfg: dict) -> str:
    """Gemini 4 Argon via the Gemini Interactions API. Single attempt, never retried."""
    key = _argon_key(cfg)
    if not key:
        raise RuntimeError("Argon needs a Gemini API key (keys.gemini or GEMINI_API_KEY)")
    model = str(cfg.get("models", {}).get("argon", "")).strip() or ARGON_MODEL
    timeout = _cli_timeout_secs(cfg, "argon", ARGON_TIMEOUT_SECS)
    body = json.dumps({"model": model, "input": prompt}).encode("utf-8")
    req = urllib.request.Request(
        ARGON_ENDPOINT,
        data=body,
        method="POST",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(f"argon API HTTP {exc.code}: {_truncate(detail.strip() or str(exc.reason), 500)}")
    except (TimeoutError, socket.timeout):
        raise RuntimeError(f"argon API timed out after {timeout}s")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"argon API timed out after {timeout}s")
        raise RuntimeError(f"argon API request failed: {exc.reason}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"argon API returned non-JSON: {_truncate(raw, 300)}")
    return _argon_response_text(parsed) or "<empty response>"


def _call_agent(agent: str, prompt: str, cfg: dict, role: str) -> str:
    if agent == "codex":
        return _call_codex(prompt, cfg, role)
    if agent == "claude":
        return _call_claude(prompt, cfg)
    if agent == "gemini":
        return _call_gemini(prompt, cfg)
    if agent == "grok":
        return _call_grok(prompt, cfg)
    if agent == "antigravity":
        return _call_antigravity(prompt, cfg)
    if agent == "argon":
        return _call_argon(prompt, cfg)
    raise RuntimeError(f"Unknown agent: {agent}")


def _known_tasks(tasks_root: Path) -> list[str]:
    if not tasks_root.exists():
        return []
    names: list[str] = []
    for p in sorted(tasks_root.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        if (p / "TASK.md").exists():
            names.append(p.name)
    return names


def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 40].rstrip() + "\n...[truncated]"


def _normalize_dir(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())


def _allowed_dirs(cfg: dict) -> list[str]:
    dirs = cfg.get("allowed_dirs", [])
    out: list[str] = []
    for d in dirs:
        try:
            n = _normalize_dir(str(d))
        except Exception:
            continue
        if n not in out:
            out.append(n)
    return out


def _is_allowed_dir(path: Path, cfg: dict) -> bool:
    target = path.resolve()
    for base_raw in _allowed_dirs(cfg):
        base = Path(base_raw)
        try:
            target.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def _cli_workdir(cfg: dict) -> str | None:
    tasks_root = Path(cfg["tasks_root"]).expanduser()
    if tasks_root.exists() and _is_allowed_dir(tasks_root, cfg):
        return str(tasks_root)
    for d in _allowed_dirs(cfg):
        p = Path(d)
        if p.exists() and p.is_dir():
            return str(p)
    return str(tasks_root) if tasks_root.exists() else None


def _kb_search(query: str, cfg: dict, limit: int = 5) -> str:
    """Search the knowledge base server if running. Returns empty string if KB unavailable."""
    port = cfg.get("kb_port", "3838")
    url = f"http://localhost:{port}/api/v1/search/smart?q={urllib.parse.quote(query)}&limit={limit}"
    try:
        headers = {"Accept": "application/json"}
        # Use API key if configured
        kb_key = cfg.get("keys", {}).get("kb", "") or os.environ.get("KB_API_KEY", "")
        if kb_key:
            headers["X-API-Key"] = kb_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read())
            # v1 API wraps results: {"results": [...]}
            data = raw.get("results", raw) if isinstance(raw, dict) else raw
            if not data or not isinstance(data, list):
                return ""
            results = []
            for doc in data[:limit]:
                title = doc.get("title", "Untitled")
                snippet = doc.get("snippet", "")[:200].replace("<mark>", "").replace("</mark>", "")
                results.append(f"- {title}: {snippet}")
            return "\n".join(results)
    except Exception:
        return ""  # KB not running — silently skip


def _shared_context(tasks_root: Path, task_id: str | None, cfg: dict | None = None) -> str:
    sections: list[str] = []
    global_files = [
        ("Global Guardrails", tasks_root / "GUARDRAILS.md"),
        ("Global Todo", tasks_root / "TODO_GLOBAL.md"),
        ("Global MCP", tasks_root / "MCP_SERVERS.md"),
        ("Shared Knowledge", tasks_root / "SHARED_KNOWLEDGE.md"),
    ]
    for label, path in global_files:
        raw = _read(path).strip()
        if raw:
            sections.append(f"## {label}\n{_truncate(raw, 5000)}")

    if task_id:
        task_dir = tasks_root / task_id
        for label, name in [
            ("Task", "TASK.md"),
            ("Task Plan", "PLAN.md"),
            ("Task Decision", "DECISION.md"),
            ("Task Todo", "TODO.md"),
            ("Task Guardrails", "GUARDRAILS.md"),
            ("Task MCP", "MCP_SERVERS.md"),
        ]:
            raw = _read(task_dir / name).strip()
            if raw:
                sections.append(f"## {label}\n{_truncate(raw, 5000)}")

    tasks = _known_tasks(tasks_root)
    if tasks:
        sections.append("## Known Tasks\n" + "\n".join(f"- {t}" for t in tasks[:200]))

    # Inject KB context if server is running
    if cfg and task_id:
        kb_results = _kb_search(task_id, cfg, limit=5)
        if kb_results:
            sections.append(f"## Knowledge Base Context\n{kb_results}")

    return "\n\n".join(sections)


def _build_prompt(
    role: str,
    user_text: str,
    history: list[dict],
    shared: str,
    name: str = "agent",
    agent: str | None = None,
    direct_address: bool = False,
) -> str:
    hist = history[-8:]
    hist_text = "\n".join(f"{m['role']}: {m['text']}" for m in hist)
    identity = _agent_prompt_identity(agent or name.lower())
    direct_note = "This message was directly addressed to you, so answer as that specific agent.\n" if direct_address else ""
    return (
        f"{identity}\n"
        f"You are operating inside {name.title()}, a pragmatic multi-agent software assistant.\n"
        f"Current role: {role}.\n"
        "Use concise, actionable outputs.\n"
        "Keep your answer in your own perspective and do not pretend to be a different agent.\n"
        f"{direct_note}"
        "Respect guardrails and todo/context.\n\n"
        "SHARED CONTEXT:\n"
        f"{shared}\n\n"
        "RECENT CHAT:\n"
        f"{hist_text}\n\n"
        "USER:\n"
        f"{user_text}\n"
    )


def _render_response_block(agent: str, role: str, text: str) -> str:
    ts = datetime.now(timezone.utc).replace(microsecond=0).strftime("%H:%M:%S")
    color = AGENT_COLORS.get(agent, "37")
    header = _c(color, f"  [{agent}]") + _c("90", f" {role} @ {ts}")
    separator = _c("90", "  " + "-" * 60)
    body = text.rstrip() or "<empty response>"
    return f"\n{header}\n{separator}\n{body}\n"


def _call_agent_with_spinner(agent: str, prompt: str, cfg: dict, role: str) -> str:
    if not sys.stdout.isatty():
        return _call_agent(agent, prompt, cfg, role)
    done = threading.Event()

    def _spin() -> None:
        frames = (".", "..", "...", "....", ".....")
        i = 0
        while not done.is_set():
            color = AGENT_COLORS.get(agent, "37")
            msg = _c(color, f"  [{agent}]") + _c("90", f" thinking{frames[i % len(frames)]}")
            sys.stdout.write(f"\r{msg}" + " " * 20)
            sys.stdout.flush()
            time.sleep(0.3)
            i += 1
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        return _call_agent(agent, prompt, cfg, role)
    finally:
        done.set()
        t.join(timeout=1)


def _route_message(raw: str) -> tuple[str, str, str | None]:
    role = "orchestrator"
    direct_agent: str | None = None
    user_text = raw.strip()
    if user_text.lower().startswith("impl:"):
        return "implementation", user_text[5:].strip(), None
    if user_text.lower().startswith("ui:"):
        return "uidocs", user_text[3:].strip(), None
    if user_text.lower().startswith("intel:"):
        return "intel", user_text[6:].strip(), None
    if user_text.lower().startswith("x:"):
        return "intel", user_text[2:].strip(), None
    lowered = user_text.lower()
    for agent_name in AGENTS:
        if agent_name in PAID_API_AGENTS:
            continue  # argon is only reachable through the review chain
        prefix = f"@{agent_name} "
        if lowered.startswith(prefix):
            direct_agent = agent_name
            user_text = user_text[len(prefix):].strip()
            role = _default_role_for_direct_agent(agent_name)
            break
    return role, user_text, direct_agent


def _run_once(cfg: dict, message: str, task_id: str | None) -> int:
    history: list[dict] = []
    role, user_text, direct_agent = _route_message(message)
    history.append({"role": "user", "text": user_text})
    try:
        agent, out, failures = _chat_once(role, user_text, cfg, history, task_id, direct_agent=direct_agent)
        sys.stdout.write(_render_response_block(agent, role, out))
        if failures:
            for f in failures:
                sys.stdout.write(_c("33", f"  [fallback] {f}") + "\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


def _smoke_test(cfg: dict, task_id: str | None) -> int:
    prompt = "Reply with exactly: smoke-ok"
    role = "orchestrator"
    history: list[dict] = [{"role": "user", "text": prompt}]
    shared = _shared_context(Path(cfg["tasks_root"]).expanduser(), task_id, cfg)
    built = _build_prompt(role, prompt, history, shared, cfg.get("name", "agent"))
    print("Smoke test start")
    rc = 0
    for agent in AGENTS:
        if agent in PAID_API_AGENTS:
            print(f"- {agent}: skipped (paid API; not smoke-tested)")
            continue
        status = _service_status(agent, cfg)
        if status.startswith("down") or status.startswith("unavailable"):
            print(f"- {agent}: skipped ({status})")
            continue
        try:
            out = _call_agent_with_spinner(agent, built, cfg, role)
            first = (out or "").strip().splitlines()
            sample = first[0] if first else "<empty response>"
            print(f"- {agent}: ok -> {sample[:120]}")
        except Exception as exc:
            rc = 1
            print(f"- {agent}: failed -> {exc}")
    print("Smoke test done")
    return rc


def _write_tasks_env(cfg: dict) -> None:
    tasks_root = Path(cfg["tasks_root"]).expanduser()
    env_path = tasks_root / ".env"
    lines = [
        f"OPENAI_API_KEY={cfg['keys']['openai']}",
        f"ANTHROPIC_API_KEY={cfg['keys']['anthropic']}",
        f"GEMINI_API_KEY={cfg['keys']['gemini']}",
        f"XAI_API_KEY={cfg['keys'].get('xai', '')}",
        "",
        f"OPENAI_MODEL_CODEX={cfg['models']['codex']}",
        f"OPENAI_MODEL_ORCHESTRATOR_FALLBACK={cfg['models']['codex_orchestrator']}",
        f"ANTHROPIC_MODEL_ORCHESTRATOR={cfg['models']['claude']}",
        f"GEMINI_MODEL_UI_DOCS={cfg['models']['gemini']}",
        f"GROK_MODEL_INTEL={cfg['models'].get('grok', 'grok-4.7')}",
        f"ARGON_MODEL_REVIEW={cfg['models'].get('argon', ARGON_MODEL)}",
        "",
        f"ORCHESTRATOR_CHAIN={','.join(cfg['chains']['orchestrator'])}",
        f"IMPLEMENTATION_CHAIN={','.join(cfg['chains']['implementation'])}",
        f"UIDOCS_CHAIN={','.join(cfg['chains']['uidocs'])}",
        f"REVIEW_CHAIN={','.join(cfg['chains']['review'])}",
        f"INTEL_CHAIN={','.join(cfg['chains'].get('intel', DEFAULTS['chains']['intel']))}",
        "SHARED_CONTEXT_MAX_SECTION_CHARS=6000",
    ]
    _write(env_path, "\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)


def _run_init_shared(tasks_root: Path) -> None:
    script = tasks_root / "orchestrator.py"
    if not script.exists():
        return
    subprocess.run([sys.executable, str(script), "init-shared"], check=False)


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val if val else default


def setup_wizard(cfg: dict, force: bool = False) -> dict:
    name = cfg.get("name", "agent")
    print(f"\n{name.title()} setup wizard")
    print("Configure API and/or CLI providers. At least one usable provider is required.\n")

    name = _prompt("Orchestrator name", name).lower().strip() or "agent"
    cfg["name"] = name
    tasks_root = Path(_prompt("Tasks root", cfg["tasks_root"]))
    cfg["tasks_root"] = str(tasks_root)
    claude_mode = _prompt("Claude mode (cli/api)", cfg.get("claude_mode", "cli")).lower()
    if claude_mode not in ("cli", "api"):
        claude_mode = "cli"
    cfg["claude_mode"] = claude_mode
    codex_mode = _prompt("Codex mode (api/cli)", cfg.get("codex_mode", "api")).lower()
    if codex_mode not in ("api", "cli"):
        codex_mode = "api"
    cfg["codex_mode"] = codex_mode
    if cfg["codex_mode"] == "cli":
        codex_perm = _prompt(
            "Codex CLI permission mode (full-host-unattended/sandboxed-auto)",
            cfg.get("codex_cli_permission_mode", "full-host-unattended"),
        ).lower()
        if codex_perm not in ("full-host-unattended", "sandboxed-auto"):
            codex_perm = "full-host-unattended"
        cfg["codex_cli_permission_mode"] = codex_perm
    gemini_mode = _prompt("Gemini mode (api/cli)", cfg.get("gemini_mode", "api")).lower()
    if gemini_mode not in ("api", "cli"):
        gemini_mode = "api"
    cfg["gemini_mode"] = gemini_mode
    if cfg["gemini_mode"] == "cli":
        gemini_perm = _prompt(
            "Gemini CLI permission mode (full-host-unattended/sandboxed-auto)",
            cfg.get("gemini_cli_permission_mode", "full-host-unattended"),
        ).lower()
        if gemini_perm not in ("full-host-unattended", "sandboxed-auto"):
            gemini_perm = "full-host-unattended"
        cfg["gemini_cli_permission_mode"] = gemini_perm
    grok_mode = _prompt("Grok mode (cli)", cfg.get("grok_mode", "cli")).lower()
    if grok_mode != "cli":
        grok_mode = "cli"
    cfg["grok_mode"] = grok_mode
    grok_perm = _prompt(
        "Grok CLI permission mode (sandboxed-auto/full-host-unattended; full-host only inside a container/VM)",
        _safe_permission_mode(cfg.get("grok_cli_permission_mode")),
    ).lower()
    cfg["grok_cli_permission_mode"] = _safe_permission_mode(grok_perm)
    cfg["antigravity_mode"] = "cli"
    agy_perm = _prompt(
        "Antigravity (agy) CLI permission mode (sandboxed-auto/full-host-unattended; full-host only inside a container/VM)",
        _safe_permission_mode(cfg.get("antigravity_cli_permission_mode")),
    ).lower()
    cfg["antigravity_cli_permission_mode"] = _safe_permission_mode(agy_perm)

    existing_openai = cfg["keys"]["openai"] if cfg["keys"]["openai"] and not force else ""
    existing_anthropic = cfg["keys"]["anthropic"] if cfg["keys"]["anthropic"] and not force else ""
    existing_gemini = cfg["keys"]["gemini"] if cfg["keys"]["gemini"] and not force else ""
    existing_xai = cfg["keys"].get("xai", "") if cfg["keys"].get("xai", "") and not force else ""

    oai_label = "OpenAI API key"
    if cfg["codex_mode"] == "cli":
        oai_label += " (optional in cli mode)"
    oai = getpass(f"{oai_label}{' [press Enter to keep existing]' if existing_openai else ''}: ").strip()
    ant_label = "Anthropic API key"
    if cfg["claude_mode"] == "cli":
        ant_label += " (optional in cli mode)"
    ant = getpass(f"{ant_label}{' [press Enter to keep existing]' if existing_anthropic else ''}: ").strip()
    gem_label = "Gemini API key"
    if cfg["gemini_mode"] == "cli":
        gem_label += " (optional in cli mode)"
    gem = getpass(f"{gem_label}{' [press Enter to keep existing]' if existing_gemini else ''}: ").strip()
    xai = getpass(
        f"xAI API key (optional; Grok CLI can also use `grok login`)"
        f"{' [press Enter to keep existing]' if existing_xai else ''}: "
    ).strip()

    cfg["keys"]["openai"] = oai or existing_openai
    cfg["keys"]["anthropic"] = ant or existing_anthropic
    cfg["keys"]["gemini"] = gem or existing_gemini
    cfg["keys"]["xai"] = xai or existing_xai

    has_provider = any(v.strip() for v in cfg["keys"].values())
    if cfg["claude_mode"] == "cli" and shutil.which("claude"):
        has_provider = True
    if cfg["codex_mode"] == "cli" and shutil.which("codex"):
        has_provider = True
    if cfg["gemini_mode"] == "cli" and shutil.which("gemini"):
        has_provider = True
    if cfg["grok_mode"] == "cli" and shutil.which("grok"):
        has_provider = True
    if cfg.get("antigravity_mode", "cli") == "cli" and shutil.which("agy"):
        has_provider = True
    if not has_provider:
        raise RuntimeError(
            "No usable provider configured. Add at least one API key, or use Claude/Codex/Gemini/Grok/Antigravity in cli mode with their CLIs installed."
        )

    print("\nModel IDs (use exact account IDs if custom):")
    cfg["models"]["claude"] = _prompt("Claude model", cfg["models"]["claude"])
    cfg["models"]["codex"] = _prompt("Codex model", cfg["models"]["codex"])
    cfg["models"]["codex_orchestrator"] = _prompt(
        "Codex fallback orchestrator model", cfg["models"]["codex_orchestrator"]
    )
    cfg["models"]["gemini"] = _prompt("Gemini model", cfg["models"]["gemini"])
    cfg["models"]["grok"] = _prompt("Grok model", cfg["models"].get("grok", "grok-4.7"))
    cfg["models"]["antigravity"] = _prompt(
        "Antigravity model (blank = agy default)", cfg["models"].get("antigravity", "")
    )
    cfg["models"]["argon"] = _prompt("Argon model (Gemini API id)", cfg["models"].get("argon", ARGON_MODEL))

    print("\nRole fallback chains (comma separated, values: claude,codex,gemini,grok,antigravity,argon; argon is honoured in REVIEW_CHAIN only):")
    cfg["chains"]["orchestrator"] = _parse_chain(
        _prompt("ORCHESTRATOR_CHAIN", ",".join(cfg["chains"]["orchestrator"])),
        DEFAULTS["chains"]["orchestrator"],
    )
    cfg["chains"]["implementation"] = _parse_chain(
        _prompt("IMPLEMENTATION_CHAIN", ",".join(cfg["chains"]["implementation"])),
        DEFAULTS["chains"]["implementation"],
    )
    cfg["chains"]["uidocs"] = _parse_chain(
        _prompt("UIDOCS_CHAIN", ",".join(cfg["chains"]["uidocs"])),
        DEFAULTS["chains"]["uidocs"],
    )
    cfg["chains"]["review"] = _parse_chain(
        _prompt("REVIEW_CHAIN", ",".join(cfg["chains"]["review"])),
        DEFAULTS["chains"]["review"],
    )
    cfg["chains"]["intel"] = _parse_chain(
        _prompt("INTEL_CHAIN", ",".join(cfg["chains"].get("intel", DEFAULTS["chains"]["intel"]))),
        DEFAULTS["chains"]["intel"],
    )
    for role_key in ("orchestrator", "implementation", "uidocs", "review", "intel"):
        cfg["chains"][role_key] = [a for a in cfg["chains"][role_key] if _agent_allowed_in_role(a, role_key)]

    _save_config(cfg)
    _write_tasks_env(cfg)
    _run_init_shared(tasks_root)
    print(f"\nSaved: {CONFIG_PATH}")
    print(f"Synced env: {tasks_root / '.env'}\n")
    return cfg


def _run_orchestrator(task_id: str, cfg: dict) -> int:
    tasks_root = Path(cfg["tasks_root"]).expanduser()
    script = tasks_root / "orchestrator.py"
    if not script.exists():
        print(f"orchestrator.py not found at {script}")
        return 1
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = cfg["keys"]["openai"]
    env["ANTHROPIC_API_KEY"] = cfg["keys"]["anthropic"]
    env["GEMINI_API_KEY"] = cfg["keys"]["gemini"]
    env["XAI_API_KEY"] = cfg["keys"].get("xai", "")
    env["OPENAI_MODEL_CODEX"] = cfg["models"]["codex"]
    env["OPENAI_MODEL_ORCHESTRATOR_FALLBACK"] = cfg["models"]["codex_orchestrator"]
    env["ANTHROPIC_MODEL_ORCHESTRATOR"] = cfg["models"]["claude"]
    env["GEMINI_MODEL_UI_DOCS"] = cfg["models"]["gemini"]
    env["GROK_MODEL_INTEL"] = cfg["models"].get("grok", "grok-4.7")
    env["ORCHESTRATOR_CHAIN"] = ",".join(cfg["chains"]["orchestrator"])
    env["IMPLEMENTATION_CHAIN"] = ",".join(cfg["chains"]["implementation"])
    env["UIDOCS_CHAIN"] = ",".join(cfg["chains"]["uidocs"])
    env["REVIEW_CHAIN"] = ",".join(cfg["chains"]["review"])
    env["INTEL_CHAIN"] = ",".join(cfg["chains"].get("intel", DEFAULTS["chains"]["intel"]))
    unsupported_for_orchestrator: set[str] = set()
    if cfg.get("claude_mode", "cli") == "cli":
        unsupported_for_orchestrator.add("claude")
    if cfg.get("codex_mode", "api") == "cli":
        unsupported_for_orchestrator.add("codex")
    if cfg.get("gemini_mode", "api") == "cli":
        unsupported_for_orchestrator.add("gemini")
    if cfg.get("grok_mode", "cli") == "cli":
        unsupported_for_orchestrator.add("grok")
    # The external orchestrator.py has no adapter for these two.
    unsupported_for_orchestrator.update(("antigravity", "argon"))
    if unsupported_for_orchestrator:
        for role_key in ("orchestrator", "implementation", "uidocs", "review", "intel"):
            chain_key = f"{role_key.upper()}_CHAIN"
            agents = [a for a in env[chain_key].split(",") if a and a not in unsupported_for_orchestrator]
            env[chain_key] = ",".join(agents)
    print(f"Running orchestrator for task '{task_id}'...")
    proc = subprocess.run([sys.executable, str(script), "run", "--task-id", task_id], env=env)
    return proc.returncode


def _chat_once(
    role: str,
    message: str,
    cfg: dict,
    history: list[dict],
    task_id: str | None,
    direct_agent: str | None = None,
) -> tuple[str, str, list[str]]:
    chain = [direct_agent] if direct_agent else _role_chain(role, cfg)
    if not chain:
        raise RuntimeError("No available agents for this role. Add at least one API key in /setup.")

    shared = _shared_context(Path(cfg["tasks_root"]).expanduser(), task_id, cfg)
    failures: list[str] = []
    for agent in chain:
        try:
            prompt = _build_prompt(
                role,
                message,
                history,
                shared,
                cfg.get("name", "agent"),
                agent=agent,
                direct_address=direct_agent is not None,
            )
            out = _call_agent_with_spinner(agent, prompt, cfg, role)
            return agent, out, failures
        except Exception as exc:
            failures.append(f"{agent}: {exc}")
            auto_note = _apply_auto_downtime(agent, str(exc), cfg)
            if auto_note:
                failures.append(auto_note)
    raise RuntimeError("All agents failed: " + " | ".join(failures))


def _print_help() -> None:
    print("Commands:")
    print("  /help                 show help")
    print("  /setup                rerun setup wizard")
    print("  /models               show models")
    print("  /modes                show CLI/API modes and permission modes")
    print("  /modes set <agent> <full-host-unattended|sandboxed-auto>")
    print("  /permissions          alias for /modes")
    print("  /permissions set <agent> <full-host-unattended|sandboxed-auto>")
    print("  /chains               show role chains")
    print("  /task                 show current task")
    print("  /task list            list known tasks")
    print("  /task <id>            switch task (auto scaffold if missing)")
    print("  /run                  run orchestrator for current task")
    print("  /smoke                run smoke test across enabled providers")
    print("  /kb <query>           search the knowledge base")
    print("  /service status       list provider status (up/down/unavailable)")
    print("  /service down <agent> <minutes|manual>")
    print("  /service up <agent>   re-enable provider immediately")
    print("  /service recover      re-enable all disabled providers")
    print("  /allow-dir list       list approved directories")
    print("  /allow-dir add <path> approve a directory subtree")
    print("  /allow-dir rm <path>  remove an approved directory")
    print("  /quit                 exit")
    print("  impl: <msg>           force implementation role")
    print("  ui: <msg>             force ui/docs role")
    print("  intel: <msg>          force intel/research role")
    print("  x: <msg>              alias for intel role")
    print("  @claude <msg>         send directly to Claude")
    print("  @codex <msg>          send directly to Codex")
    print("  @gemini <msg>         send directly to Gemini")
    print("  @grok <msg>           send directly to Grok")
    print("  @antigravity <msg>    send directly to Antigravity (agy)")


def run_chat(cfg: dict, initial_task: str | None) -> int:
    task_id = initial_task
    history: list[dict] = []
    name = cfg.get("name", "agent")
    print(f"{_c('1', name.title())} ready. Type /help for commands.")

    while True:
        try:
            raw = input(_c("1", f"{name}> ") if _COLORS else f"{name}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not raw:
            continue
        if raw == "/quit":
            return 0
        if raw == "/help":
            _print_help()
            continue
        if raw == "/setup":
            try:
                cfg = setup_wizard(cfg, force=False)
            except Exception as exc:
                print(f"Setup failed: {exc}")
            continue
        if raw == "/models":
            payload = {
                "claude_mode": cfg.get("claude_mode", "cli"),
                "codex_mode": cfg.get("codex_mode", "api"),
                "gemini_mode": cfg.get("gemini_mode", "api"),
                "grok_mode": cfg.get("grok_mode", "cli"),
                "antigravity_mode": cfg.get("antigravity_mode", "cli"),
                "models": cfg["models"],
            }
            print(json.dumps(payload, indent=2))
            continue
        if raw in ("/modes", "/permissions"):
            print(json.dumps(_cli_permissions_payload(cfg), indent=2))
            continue
        if raw.startswith("/modes set ") or raw.startswith("/permissions set "):
            parts = raw.split()
            if len(parts) != 4:
                print("Usage: /permissions set <codex|gemini|grok|antigravity> <full-host-unattended|sandboxed-auto>")
                continue
            _, _, agent, permission_mode = parts
            ok, msg = _set_cli_permission_mode(cfg, agent, permission_mode)
            print(msg)
            continue
        if raw == "/chains":
            print(json.dumps(cfg["chains"], indent=2))
            continue
        if raw == "/smoke":
            rc = _smoke_test(cfg, task_id)
            print(f"smoke exit code: {rc}")
            continue
        if raw.startswith("/kb "):
            query = raw[4:].strip()
            if not query:
                print("Usage: /kb <search query>")
                continue
            results = _kb_search(query, cfg, limit=10)
            if results:
                print(f"\n{_c('36', 'KB results:')}\n{results}\n")
            else:
                print(_c("33", "No results (KB server may not be running)"))
            continue
        if raw == "/service recover":
            recovered = []
            for agent in AGENTS:
                override = cfg["service_overrides"][agent]
                if override.get("disabled_until") or override.get("manual_disabled"):
                    override["manual_disabled"] = False
                    override["disabled_until"] = ""
                    recovered.append(agent)
            if recovered:
                _try_save_config(cfg)
                print(f"Recovered: {', '.join(recovered)}")
            else:
                print("All agents already up.")
            continue
        if raw == "/service status":
            changed = False
            for agent in AGENTS:
                before = cfg["service_overrides"][agent].get("disabled_until", "")
                status = _service_status(agent, cfg)
                after = cfg["service_overrides"][agent].get("disabled_until", "")
                if before != after:
                    changed = True
                print(f"- {agent}: {status}")
            if changed:
                ok, err = _try_save_config(cfg)
                if not ok:
                    print(f"warning: could not persist config: {err}")
            continue
        if raw == "/allow-dir list":
            dirs = _allowed_dirs(cfg)
            if not dirs:
                print("No approved directories.")
            else:
                for d in dirs:
                    print(f"- {d}")
            continue
        if raw.startswith("/allow-dir add "):
            path_raw = raw.split(" ", 2)[2].strip()
            if not path_raw:
                print("Usage: /allow-dir add <path>")
                continue
            try:
                normalized = _normalize_dir(path_raw)
            except Exception as exc:
                print(f"invalid path: {exc}")
                continue
            dirs = _allowed_dirs(cfg)
            if normalized not in dirs:
                dirs.append(normalized)
            cfg["allowed_dirs"] = dirs
            ok, err = _try_save_config(cfg)
            if not ok:
                print(f"warning: could not persist config: {err}")
            print(f"approved: {normalized}")
            continue
        if raw.startswith("/allow-dir rm "):
            path_raw = raw.split(" ", 2)[2].strip()
            if not path_raw:
                print("Usage: /allow-dir rm <path>")
                continue
            try:
                normalized = _normalize_dir(path_raw)
            except Exception as exc:
                print(f"invalid path: {exc}")
                continue
            dirs = [d for d in _allowed_dirs(cfg) if d != normalized]
            cfg["allowed_dirs"] = dirs
            ok, err = _try_save_config(cfg)
            if not ok:
                print(f"warning: could not persist config: {err}")
            print(f"removed: {normalized}")
            continue
        if raw.startswith("/service down "):
            parts = raw.split()
            if len(parts) != 4:
                print("Usage: /service down <claude|codex|gemini|grok|antigravity|argon> <minutes|manual>")
                continue
            _, _, agent, value = parts
            if agent not in AGENTS:
                print("Unknown agent. Use claude, codex, gemini, grok, antigravity, or argon.")
                continue
            override = cfg["service_overrides"][agent]
            if value == "manual":
                override["manual_disabled"] = True
                override["disabled_until"] = ""
                ok, err = _try_save_config(cfg)
                if not ok:
                    print(f"warning: could not persist config: {err}")
                print(f"{agent} set to manual down. Use /service up {agent} to re-enable.")
                continue
            try:
                minutes = int(value)
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                print("Minutes must be a positive integer, or use 'manual'.")
                continue
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            override["manual_disabled"] = False
            override["disabled_until"] = _format_iso_utc(until)
            ok, err = _try_save_config(cfg)
            if not ok:
                print(f"warning: could not persist config: {err}")
            print(f"{agent} down until {override['disabled_until']}")
            continue
        if raw.startswith("/service up "):
            parts = raw.split()
            if len(parts) != 3:
                print("Usage: /service up <claude|codex|gemini|grok|antigravity|argon>")
                continue
            _, _, agent = parts
            if agent not in AGENTS:
                print("Unknown agent. Use claude, codex, gemini, grok, antigravity, or argon.")
                continue
            override = cfg["service_overrides"][agent]
            override["manual_disabled"] = False
            override["disabled_until"] = ""
            ok, err = _try_save_config(cfg)
            if not ok:
                print(f"warning: could not persist config: {err}")
            print(f"{agent} re-enabled.")
            continue
        if raw == "/task":
            print(f"Current task: {task_id or '(none)'}")
            continue
        if raw == "/task list":
            names = _known_tasks(Path(cfg["tasks_root"]))
            if not names:
                print("No tasks found.")
            else:
                for n in names:
                    print(f"- {n}")
            continue
        if raw.startswith("/task "):
            requested = raw.split(" ", 1)[1].strip()
            if not requested:
                print("Usage: /task <id>")
                continue
            task_dir = Path(cfg["tasks_root"]) / requested
            if not task_dir.exists():
                script = Path(cfg["tasks_root"]) / "orchestrator.py"
                if script.exists():
                    subprocess.run([sys.executable, str(script), "scaffold", "--task-id", requested], check=False)
            task_id = requested
            print(f"Switched task: {task_id}")
            continue
        if raw == "/run":
            if not task_id:
                print("Set a task first with /task <id>")
                continue
            rc = _run_orchestrator(task_id, cfg)
            print(f"orchestrator exit code: {rc}")
            continue

        role, user_text, direct_agent = _route_message(raw)

        history.append({"role": "user", "text": user_text})
        try:
            agent, out, failures = _chat_once(role, user_text, cfg, history, task_id, direct_agent=direct_agent)
            print(_render_response_block(agent, role, out))
            if failures:
                for f in failures:
                    print(_c("33", f"  [fallback] {f}"))
                print()
            history.append({"role": "assistant", "text": out})
            history[:] = history[-20:]
        except Exception as exc:
            print(f"error: {exc}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-agent terminal orchestrator")
    parser.add_argument("--setup", action="store_true", help="run setup wizard and exit")
    parser.add_argument("--task", default="", help="initial task id")
    parser.add_argument("--prompt", default="", help="run a single prompt non-interactively and exit")
    args = parser.parse_args()

    cfg = _load_config()
    if args.setup or not CONFIG_PATH.exists():
        try:
            cfg = setup_wizard(cfg, force=False)
        except Exception as exc:
            print(f"Setup failed: {exc}")
            return 1
        if args.setup:
            return 0

    if args.prompt:
        return _run_once(cfg, args.prompt, args.task or None)

    return run_chat(cfg, args.task or None)


if __name__ == "__main__":
    raise SystemExit(main())
