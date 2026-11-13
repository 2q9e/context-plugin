# Context Window Manager

A local-first task and recovery layer for coding agents. It keeps a per-project SQLite checklist, reconciles new prompt clauses without deleting prior work, records checkpoints, and exposes the same state through a CLI, host hooks, and MCP tools.

It cannot increase a model's physical context limit. The core uses a character-bounded, lexical retrieval bundle; automatic hooks capture prompt text, while semantic interpretation still depends on the model following the supplied workflow or calling the MCP tools.

## Features

- Project-isolated SQLite state in `.context-manager/state.sqlite3` with WAL mode, transactions, and foreign keys.
- Tasks with IDs, statuses, priority, parent, dependencies, acceptance criteria, relevant project files, decisions, verification evidence, and timestamps.
- Prompt-clause capture with exact normalized deduplication, append-on-new-requirement behavior, explicit change/cancel reconciliation, and common credential redaction.
- Session checkpoints and restart recovery, ranked lexical retrieval, and bounded context output.
- Completion requires a named evidence source and result, such as `test: python -m unittest passed`; evidence is recorded but not independently validated by the tool. Dependencies must be complete first.
- MCP stdio server and examples for Claude Code, Codex, Cursor, and Freebuff.
- Optional capture opt-out and history retention; active checklist tasks are retained.

## Install

Requires Python 3.10+ and SQLite from the Python standard library. No runtime dependencies are required.

```sh
python -m pip install .
cwm init
cwm sync "Build the login page. Add password reset."
cwm list
cwm recover
```

For development, run `python -m unittest discover -v` from the repository root.

## Usage

```sh
cwm add "Implement OAuth login" --priority 1 --accept "Login succeeds" --accept "Invalid credentials are rejected"
cwm list
cwm depends TASK_ID OTHER_TASK_ID
cwm decision TASK_ID "Use the existing OAuth library"
cwm files TASK_ID src/auth.py tests/test_auth.py
cwm status TASK_ID in_progress
cwm status TASK_ID completed --evidence "test: pytest tests/test_auth.py passed"
cwm checkpoint "OAuth flow implemented" --last "Added callback handler" --next "Verify provider errors"
cwm context "OAuth callback" --chars 4000
cwm recover --budget 10
```

The automatic `sync` operation stores sanitized prompt clauses as candidate tasks. It is intentionally conservative: it cannot reliably determine which sentences are requirements, decisions, or discussion. The agent should compare each incoming message against existing work and call the MCP `sync_requirements` tool with any semantic additions, revisions, and cancellations it identifies. A changed requirement is reset to pending and has an event entry preserving the previous description.

## Host integrations

Install the package in the same environment as the host so `cwm` and `cwm-mcp` are on `PATH`. Copy the relevant config example into the host's documented config location and merge it with existing settings rather than replacing them.

### Claude Code

Merge [`integrations/claude/hooks.json`](integrations/claude/hooks.json) into `.claude/settings.json` (shared project) or `~/.claude/settings.json` (user scope). The UserPromptSubmit hook captures each prompt and adds the active checklist as `additionalContext`; SessionStart restores context; PreCompact saves a checkpoint. Claude Code terminal, IDE, desktop, and cloud sessions use their respective settings availability. See the [Claude Code hooks reference](https://code.claude.com/docs/en/hooks).

For MCP, merge the `mcpServers` entry from [`integrations/claude/mcp.json`](integrations/claude/mcp.json) into `.mcp.json`:

```json
{"mcpServers":{"context-window-manager":{"command":"cwm-mcp","args":[]}}}
```

### Codex

Copy [`integrations/codex/hooks.json`](integrations/codex/hooks.json) to `.codex/hooks.json` in the project or `~/.codex/hooks.json`. Codex runs commands from the session working directory. UserPromptSubmit injects the active task bundle, SessionStart recovers it, and PreCompact checkpoints. Hook trust review may be required. Codex plugin-directory publication currently excludes lifecycle hooks, so use the project/user hooks configuration or a manually installed plugin. See [Codex hooks](https://developers.openai.com/codex/hooks) and [plugin packaging limits](https://developers.openai.com/plugins/guides/submit-claude-plugin).

For MCP, merge [`integrations/codex/config.toml`](integrations/codex/config.toml) into the project's `.codex/config.toml` or `~/.codex/config.toml`. Keep hook and MCP settings in separate files if the Codex version warns about duplicate hook declarations.

### Cursor

Merge [`integrations/cursor/hooks.json`](integrations/cursor/hooks.json) into `.cursor/hooks.json` in the project or `~/.cursor/hooks.json`. `beforeSubmitPrompt` captures prompt text, `sessionStart` can provide startup context, and `preCompact` checkpoints. Cursor's `beforeSubmitPrompt` response can allow or block submission but does not inject context; `sessionStart` is fire-and-forget, so context may arrive after the first response. Mid-session retrieval uses the MCP tools or repository instructions. Cloud-agent support differs; project hook configs are used when the agent has a writable environment. See [Cursor hooks](https://cursor.com/docs/hooks).

For MCP, merge [`integrations/cursor/mcp.json`](integrations/cursor/mcp.json) into `.cursor/mcp.json` or the user MCP configuration.

### Freebuff

Freebuff does not expose a general third-party lifecycle hook for every chat. Copy [`integrations/freebuff/context-manager.ts`](integrations/freebuff/context-manager.ts) into your project's `.agents/` directory and ensure the `types/agent-definition` helper is available there. The example registers the local stdio MCP server and injects the checklist workflow after each user input. This works when that custom agent is selected; it does not intercept arbitrary Freebuff sessions automatically. An SDK wrapper can use the same MCP tools. See [custom agent definitions](https://www.codebuff.com/docs/agents/creating-new-agents) and the [SDK guide](https://www.codebuff.com/docs/advanced/sdk).

## Privacy and retention

All data stays local by default. The store redacts common API keys, bearer tokens, passwords, and access tokens before persistence. Redaction patterns cannot recognize every secret format; review inputs and avoid deliberately sending secrets to hooks. Set `CWM_DISABLE_CAPTURE=1` to disable prompt capture while leaving manual task and recovery commands usable. Set `CWM_RETENTION_DAYS=N` to expire event history and checkpoints older than N days; task requirements are preserved. Delete `.context-manager/` to remove all project state. Completion evidence is self-attested and stored; the tool does not run the referenced test or verify the files itself.

## Architecture

`context_manager/store.py` owns transactional SQLite persistence, requirement reconciliation, dependency validation, privacy filters, context selection, and recovery. `cli.py` implements direct use and host hook responses. `mcp_server.py` offers stdio tools using the same database. Host configs remain thin and optional. The core uses a lexical token-overlap scorer, not embeddings or a language model; `--chars` limits serialized context size, not true model tokens.

See [`docs/REFERENCE_RESEARCH.md`](docs/REFERENCE_RESEARCH.md) for source-level reference notes and [`DEVELOPMENT_CHECKLIST.md`](DEVELOPMENT_CHECKLIST.md) for the implementation status.

## Troubleshooting

- `cwm: command not found`: install with `python -m pip install .` and ensure that Python's scripts directory is on `PATH`.
- No recovered tasks: ensure the host hook runs with the project root as its working directory; inspect `.context-manager/state.sqlite3` with `cwm list`.
- MCP server does not start: run `cwm-mcp` in a terminal. It speaks JSON-RPC over stdin/stdout; diagnostics should be sent to stderr by the host.
- Cursor mid-session prompt was captured but no context appeared: this is a host hook output limitation; call `search_context` or enable the MCP server.
- Shared folder or worktree use: each root gets its own database by design. To share state across worktrees, configure the same absolute DB path only in a trusted private setup (not currently supported through project config).

## License

MIT. See [`LICENSE`](LICENSE).
