# Persistent project context

When the Context Window Manager MCP server is connected, use it at the start of each user turn:

1. Compare the latest message with the persistent checklist. Add new requirements, revise affected tasks, and cancel only tasks the user explicitly cancels. Preserve unrelated unfinished work.
2. Treat automatic hook capture as a candidate checklist, not semantic proof that every requirement has been extracted. Call `sync_requirements` with any requirements or changes the hook did not capture.
3. Call `recover_context` after a new session or context reset. Retrieve only relevant details with `search_context`.
4. Update task statuses as work progresses. Mark work complete only with concrete verification such as a test command and result, a reviewed file, or a commit.
5. Save a checkpoint before compaction or ending work when the host does not do it automatically.

The store is project-local under `.context-manager/`. Do not include secrets or unrelated project data in task descriptions, decisions, or checkpoints.
