# Development checklist

This checklist tracks the repository implementation against the assignment. Each checked item has implementation or validation evidence in the repository.

- [x] Inspect the target repository through the GitHub connector; it began with only a one-line README.
- [x] Research persistence, task planning, retrieval, Freebuff integration, and host extension mechanisms; see `docs/REFERENCE_RESEARCH.md`.
- [x] Implement local SQLite persistence, project isolation, task statuses, priority, parent/dependency links, decisions, relevant files, acceptance criteria, verification records, and checkpoints.
- [x] Implement candidate prompt capture, deduplication, append-on-new behavior, explicit requirement changes/cancellations, credential redaction, and capture opt-out.
- [x] Implement bounded lexical retrieval, session recovery, privacy retention controls, and dependency completion checks.
- [x] Implement the CLI and MCP stdio server over the same project store.
- [x] Add an Obsidian-compatible project notes vault with Markdown properties/tags, aliases, wikilinks and Markdown links, backlinks, unlinked mentions, property/tag search, and CLI/MCP access; installed-package smoke checks cover create/search/link/backlink flows.
- [x] Extend human-curated knowledge support from Obsidian patterns: create and resolve aliases, read YAML/JSON frontmatter and variable-indentation flat lists, search scalar/flat-list properties, resolve Markdown inline/reference links, and review unlinked title/alias mentions. Add regression coverage for CLI/MCP-facing vault behavior.
- [x] Add routine state cleanup for retention, abandoned note-write temp files, and SQLite compaction; add an explicitly confirmed full purge while preserving notes during routine cleanup. An installed-package smoke check verified stale-temp removal, compaction, preservation, purge, and symlink refusal.
- [x] Add Claude Code, Codex, Cursor, and Freebuff integration examples with documented platform limits.
- [x] Verify JSON/TOML examples parse and compare host examples with current Claude Code, Codex, Cursor, and Codebuff documentation. CLI hook outputs were exercised, and Codex CLI resolved the trusted-project MCP config; host hook events were not launched in those applications.
- [x] Run the automated suite: 37 tests passed on 2026-11-16, including the reset/recovery end-to-end simulation, relevant-path secret redaction, and curated-vault behavior.
- [x] Install the package into a temporary no-network virtual environment and exercise installed CLI and `cwm-mcp` entry points; verify modern discovery/tool calls and legacy initialization/tool listing. Python 3.10 minimum-version runtime compatibility was not separately exercised.
- [x] Complete independent code audit and resolve the context-budget loop, Cursor preCompact schema, dependency enforcement, and evidence validation findings.
- [x] Audit and improve transactional task writes, indexed requirement/history lookups, iterative dependency checks, project-relative path filtering, Markdown code-span parsing, and vault link-resolution scans; align CLI/MCP root selection through `CWM_PROJECT_ROOT`; add dual-era MCP discovery/version negotiation. The original increment had no dedicated tests; current verification now covers the relevant flows.
- [x] Redact credential-like values from persisted relevant-file paths; add a regression test for the uncovered persistence path.
- [x] Commit each implementation increment separately through the GitHub connector and push to `main`.
- [x] Reconcile the local checkout with the pushed GitHub `main` branch and verify a clean working tree.
- [x] Review limitations: semantic extraction remains model-assisted; retrieval is lexical; the tool records but does not independently run verification; Beads source was unavailable through the connector.
