# Development checklist

This checklist tracks the repository implementation against the assignment. Each checked item has implementation or validation evidence in the repository.

- [x] Inspect the target repository through the GitHub connector; it began with only a one-line README.
- [x] Research persistence, task planning, retrieval, Freebuff integration, and host extension mechanisms; see `docs/REFERENCE_RESEARCH.md`.
- [x] Implement local SQLite persistence, project isolation, task statuses, priority, parent/dependency links, decisions, relevant files, acceptance criteria, verification records, and checkpoints.
- [x] Implement candidate prompt capture, deduplication, append-on-new behavior, explicit requirement changes/cancellations, credential redaction, and capture opt-out.
- [x] Implement bounded lexical retrieval, session recovery, privacy retention controls, and dependency completion checks.
- [x] Implement the CLI and MCP stdio server over the same project store.
- [x] Add Claude Code, Codex, Cursor, and Freebuff integration examples with documented platform limits.
- [x] Verify JSON/TOML examples parse and check current Claude Code, Codex, Cursor, and Freebuff hook/agent schemas.
- [x] Run the automated suite: 15 tests passed, including the reset/recovery end-to-end simulation.
- [x] Verify CLI startup, MCP initialization/tool listing, and build a wheel with the standard setuptools backend.
- [x] Complete independent code audit and resolve the context-budget loop, Cursor preCompact schema, dependency enforcement, and evidence validation findings.
- [x] Commit each implementation increment separately through the GitHub connector and push to `main`.
- [x] Reconcile the local checkout with the pushed GitHub `main` branch and verify a clean working tree.
- [x] Review limitations: semantic extraction remains model-assisted; retrieval is lexical; the tool records but does not independently run verification; Beads source was unavailable through the connector.
