# agent-orchestrator

**Three AI agents. One brain. Zero downtime.**

A terminal-based multi-agent orchestrator that wraps Claude, Codex, and Gemini CLIs with automatic failover. When one agent hits its limit, the next one picks up seamlessly. All agents share context through a knowledge base server. $60/month for three premium AI agents — no API billing.

## How It Works

```
You type a message
       |
       v
  [Orchestrator]
       |
       +---> Try Agent 1 (Claude) ---> Success? Return response
       |            |
       |         Failed/Down
       |            |
       +---> Try Agent 2 (Codex) ---> Success? Return response
       |            |
       |         Failed/Down
       |            |
       +---> Try Agent 3 (Gemini) --> Success? Return response
       |
  All failed? Show error + recovery options
```

Each message is routed through a **role chain** — an ordered list of agents to try. Roles include:

- **orchestrator** — planning, architecture, high-level reasoning (default: Claude > Codex > Gemini)
- **implementation** — code execution, build tasks (default: Codex > Claude > Gemini)
- **uidocs** — frontend, design, documentation (default: Gemini > Codex > Claude)
- **review** — code review, validation (default: Claude > Codex > Gemini)

Every role, chain order, and model is configurable.

## Features

- **Next-man-up failover** — if an agent is down, the next one in the chain takes over automatically
- **Auto-downtime detection** — detects rate limits, quota exhaustion, auth failures, and auto-disables agents with cooldown timers
- **CLI wrapping** — uses your existing Claude/Codex/Gemini CLI subscriptions, not per-token API billing
- **Full-host unattended CLI defaults** — Codex and Gemini can run headlessly with full permissions on trusted machines
- **API mode available** — switch any agent to API mode if you prefer
- **Configurable name** — call it whatever you want, not hardcoded
- **Knowledge base integration** — searches your KB server for relevant context before every response
- **Colored output** — each agent has its own color for easy identification
- **Role-based routing** — `impl:` prefix routes to implementation chain, `ui:` to UI chain
- **Task management** — switch between project contexts with `/task`
- **Service management** — manually disable/enable agents, set timed downtimes
- **Smoke testing** — test all agents with one command
- **Directory whitelisting** — control which directories agents can access

## Quickstart

```bash
git clone https://github.com/willynikes2/agent-orchestrator.git
cd agent-orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 daniel.py --setup
```

The setup wizard asks:
1. What to name your orchestrator (default: "agent")
2. Which mode for each agent (CLI or API)
3. CLI permission mode for Codex and Gemini (`full-host-unattended` or `sandboxed-auto`)
4. API keys (optional in CLI mode)
5. Model IDs
6. Role chain preferences

Then just run:
```bash
python3 daniel.py
```

For reliable one-shot scripted usage, use `--prompt` instead of piping lines into the REPL:
```bash
python3 daniel.py --prompt "@gemini review this component"
python3 daniel.py --task my-task --prompt "@codex implement the TODO in TASK.md"
```

Or install globally:
```bash
ln -s $(pwd)/daniel.py ~/.local/bin/agent-orchestrator
```

## Prerequisites

- Python 3.10+
- At least one AI CLI installed:
  - `claude` — [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
  - `codex` — [OpenAI Codex CLI](https://github.com/openai/codex)
  - `gemini` — [Google Gemini CLI](https://github.com/google-gemini/gemini-cli)

## Commands

```
/help                 Show all commands
/setup                Rerun setup wizard
/models               Show current model configuration
/modes                Show CLI/API modes and permission modes
/modes set <agent> <full-host-unattended|sandboxed-auto>
/permissions          Alias for /modes
/permissions set <agent> <full-host-unattended|sandboxed-auto>
/chains               Show role fallback chains
/task                 Show current task
/task list            List known tasks
/task <id>            Switch task context
/run                  Run orchestrator script for current task
/smoke                Smoke test all enabled agents
/kb <query>           Search the knowledge base
/service status       Show agent status (up/down/unavailable)
/service down <agent> <minutes|manual>   Disable an agent
/service up <agent>   Re-enable an agent
/service recover      Re-enable ALL disabled agents
/allow-dir list       List approved directories
/allow-dir add <path> Approve a directory
/allow-dir rm <path>  Remove an approved directory
/quit                 Exit

impl: <message>       Route to implementation chain
ui: <message>         Route to UI/docs chain
@claude <message>     Send directly to Claude
@codex <message>      Send directly to Codex
@gemini <message>     Send directly to Gemini
```

## Cost Model

| Service | Monthly Cost | What You Get |
|---------|-------------|-------------|
| Claude Pro | ~$20 | Claude Code CLI with Opus |
| OpenAI subscription | ~$20 | Codex CLI |
| Google subscription | ~$20 | Gemini CLI |
| **Total** | **~$60** | **Three premium AI agents** |

No per-token API billing. CLI wrapping uses your subscription tiers. When one agent hits its usage cap, the orchestrator automatically routes to the next available agent.

## Knowledge Base Integration

If you're running [knowledge-base-server](https://github.com/willynikes2/knowledge-base-server), the orchestrator automatically searches it for relevant context before each response.

```bash
# Search KB manually
/kb docker networking

# KB context is auto-injected when you have an active task
/task my-project
> How should I set up the reverse proxy?
# Agent receives KB results for "my-project" as context
```

Set `kb_port` in config if your KB server runs on a non-default port.

## Direct Agent Addressing

You can bypass the normal role/failover chain and talk to a specific agent directly:

```bash
@codex what do you think about this refactor?
@gemini review this UI copy
@claude review what Codex and Gemini said
```

Direct addressing keeps the response labeled by agent, but skips automatic routing to the other providers unless you ask them separately.

## Configuration

Config lives at `~/.config/agent-orchestrator/config.json`:

```json
{
  "name": "agent",
  "tasks_root": "~/tasks",
  "kb_port": "3838",
  "claude_mode": "cli",
  "codex_mode": "cli",
  "gemini_mode": "cli",
  "codex_cli_permission_mode": "full-host-unattended",
  "gemini_cli_permission_mode": "full-host-unattended",
  "models": {
    "claude": "claude-sonnet-4-5",
    "codex": "gpt-5",
    "gemini": "gemini-2.5-pro"
  },
  "chains": {
    "orchestrator": ["claude", "codex", "gemini"],
    "implementation": ["codex", "claude", "gemini"],
    "uidocs": ["gemini", "codex", "claude"],
    "review": ["claude", "codex", "gemini"]
  }
}
```

Everything is configurable. Change the name, reorder chains, swap models, switch between CLI and API mode per agent.

At runtime, you can inspect or change Codex/Gemini CLI permission modes without rerunning setup:

```bash
/modes
/permissions set codex sandboxed-auto
/permissions set gemini full-host-unattended
```

`full-host-unattended` is the default for Codex and Gemini CLI mode. On trusted machines, this means:
- Codex uses `--dangerously-bypass-approvals-and-sandbox`
- Gemini uses `--approval-mode yolo --sandbox=false`

If you want sandboxed automatic execution instead, switch either permission mode to `sandboxed-auto`.

## Safety Note

The default CLI permission mode assumes you trust the host machine and the projects you point the agents at. In `full-host-unattended`, Codex and Gemini can act without pausing for human approval. Use `sandboxed-auto` if you want lower-friction automation with sandbox boundaries still enforced.

## Auto-Downtime Detection

The orchestrator automatically detects when agents are unavailable:

| Error Pattern | Action | Recovery |
|--------------|--------|----------|
| Claude usage cap exhausted | Disable until reset time | Auto-recovers at reset |
| Rate limit (any agent) | 5-minute cooldown | Auto-recovers |
| Auth failure | Manual disable | `/service up <agent>` |
| Model not found | Manual disable | Fix model, `/service up` |
| CLI not found | Marked unavailable | Install CLI |

Use `/service recover` to re-enable all disabled agents at once.

## Architecture

```
daniel.py (single file, ~1100 lines)
  |
  +-- Config: ~/.config/agent-orchestrator/config.json
  |
  +-- Agent Calls:
  |     +-- _call_claude_cli()  / _call_claude_api()
  |     +-- _call_codex_cli()   / _call_codex_api()
  |     +-- _call_gemini_cli()  / _call_gemini_api()
  |
  +-- Failover: _role_chain() -> _chat_once() -> try each agent
  |
  +-- Auto-downtime: _apply_auto_downtime() detects errors
  |
  +-- Context: _shared_context() + _kb_search()
  |
  +-- Output: colored, per-agent formatting
```

Single-file design is intentional. Easy to understand, easy to modify, easy to deploy.

## Extending

Want to add a new agent? Three steps:

1. Add it to the `AGENTS` tuple
2. Write `_call_newagent_cli()` and `_call_newagent_api()` functions
3. Add it to `_call_agent()` dispatcher and default chains

See the existing agent functions for the pattern. Each is ~30 lines.

## License

MIT

## Author

Built by [Shawn Daniel](https://github.com/willynikes2) — powered by the agents it orchestrates.
