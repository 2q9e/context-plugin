"""Command line and host hook entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from .store import project_store
from .vault import ProjectVault


def _hook_text() -> str:
    if not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
            if isinstance(payload, dict):
                return str(payload.get("prompt") or payload.get("user_message") or payload.get("message") or "")
        except (json.JSONDecodeError, OSError):
            pass
    return os.environ.get("CWM_PROMPT", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cwm", description="Persist agent requirements and recover project context")
    parser.add_argument("--root", help="Project root; defaults to the current working directory")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("init", help="Initialize project-local state")
    sync = subs.add_parser("sync", help="Record requirements from a prompt; reads host hook JSON from stdin when omitted")
    sync.add_argument("text", nargs="?")
    hook = subs.add_parser("hook", help="Host hook: read prompt JSON from stdin, sync it, and print host response")
    hook.add_argument("--host", choices=["claude", "codex", "cursor", "freebuff"], default="generic")
    hook.add_argument("--event", default="UserPromptSubmit")
    listing = subs.add_parser("list", help="List saved tasks")
    listing.add_argument("--all", action="store_true")
    done = subs.add_parser("status", help="Update a task state; completion requires evidence")
    done.add_argument("task_id"); done.add_argument("status", choices=["pending", "in_progress", "blocked", "completed", "cancelled"])
    done.add_argument("--evidence", default="")
    cp = subs.add_parser("checkpoint", help="Save a recovery checkpoint")
    cp.add_argument("summary"); cp.add_argument("--last", default=""); cp.add_argument("--next", default="")
    recover = subs.add_parser("recover", help="Print session recovery context as JSON")
    recover.add_argument("--budget", type=int, default=12)
    context = subs.add_parser("context", help="Retrieve tasks relevant to a query")
    context.add_argument("query"); context.add_argument("--limit", type=int, default=8); context.add_argument("--chars", type=int, default=6000)
    task = subs.add_parser("add", help="Add one task with optional acceptance criteria")
    task.add_argument("description"); task.add_argument("--priority", type=int, default=2); task.add_argument("--accept", action="append", default=[]); task.add_argument("--parent")
    deps = subs.add_parser("depends", help="Set dependency task IDs for a task")
    deps.add_argument("task_id"); deps.add_argument("dependencies", nargs="*")
    decision = subs.add_parser("decision", help="Record a decision on a task")
    decision.add_argument("task_id"); decision.add_argument("text")
    files = subs.add_parser("files", help="Attach project-relative files to a task")
    files.add_argument("task_id"); files.add_argument("paths", nargs="+")
    vault = subs.add_parser("vault", help="Manage Obsidian-compatible project notes")
    vault_subs = vault.add_subparsers(dest="vault_action", required=True)
    vault_subs.add_parser("path", help="Print the project vault directory")
    vault_subs.add_parser("list", help="List project notes")
    vault_list = vault_subs.choices["list"]
    vault_list.add_argument("--tag")
    vault_list.add_argument("--limit", type=int, default=100)
    vault_search = vault_subs.add_parser("search", help="Search project notes")
    vault_search.add_argument("query"); vault_search.add_argument("--limit", type=int, default=8)
    vault_read = vault_subs.add_parser("read", help="Read a note by ID, title, or vault-relative path")
    vault_read.add_argument("reference")
    vault_read.add_argument("--chars", type=int, default=20_000)
    vault_create = vault_subs.add_parser("create", help="Create a Markdown note")
    vault_create.add_argument("title")
    content_source = vault_create.add_mutually_exclusive_group()
    content_source.add_argument("--content", default="")
    content_source.add_argument("--content-file")
    vault_create.add_argument("--tag", action="append", default=[])
    vault_append = vault_subs.add_parser("append", help="Append Markdown to a note")
    vault_append.add_argument("reference"); vault_append.add_argument("content")
    vault_link = vault_subs.add_parser("link", help="Link two notes with an Obsidian wikilink")
    vault_link.add_argument("source"); vault_link.add_argument("target")
    vault_links = vault_subs.add_parser("links", help="List a note's outgoing wikilinks")
    vault_links.add_argument("reference")
    vault_backlinks = vault_subs.add_parser("backlinks", help="List notes that link to a note")
    vault_backlinks.add_argument("reference")
    args = parser.parse_args(argv)
    store = project_store(args.root)
    try:
        if args.command == "init":
            print(store.path); return 0
        if args.command in {"sync", "hook"}:
            text = args.text if args.command == "sync" and args.text is not None else _hook_text()
            result = store.sync_requirements(text) if text.strip() else {"added": [], "unchanged": [], "updated": [], "cancelled": []}
            if args.command == "hook":
                event_input = text
                if args.event in {"PreCompact", "preCompact"}:
                    store.checkpoint("Automatic pre-compaction checkpoint", event_input[:1000], "Run cwm recover and continue pending tasks")
                bundle = store.context_bundle(event_input, 5000)
                context = "Persistent project checklist:\n" + "\n".join(f"- [{t['status']}] {t['description']} (id {t['id']})" for t in bundle["tasks"])
                if bundle.get("notes"):
                    context += "\nRelevant project notes:\n" + "\n".join(
                        f"- [[{note['title']}]] ({note['path']}): {note['excerpt']}" for note in bundle["notes"]
                    )
                if bundle["checkpoint"]:
                    context = f"Last checkpoint: {bundle['checkpoint']['summary']}\nNext: {bundle['checkpoint']['next_action']}\n" + context
                if args.host == "claude" and args.event == "PreCompact":
                    # Claude Code treats PreCompact stdout as compaction instructions,
                    # not as the JSON hook response used by other events.
                    output = context
                elif args.host == "claude":
                    output = {"hookSpecificOutput": {"hookEventName": args.event, "additionalContext": context}}
                elif args.host == "codex" and args.event == "PreCompact":
                    # Codex PreCompact does not accept hook-specific additionalContext.
                    # The persisted checkpoint is available to SessionStart/recovery.
                    output = {}
                elif args.host == "codex":
                    output = {"hookSpecificOutput": {"hookEventName": args.event, "additionalContext": context}}
                elif args.host == "cursor":
                    if args.event == "sessionStart":
                        output = {"additional_context": context}
                    elif args.event == "beforeSubmitPrompt":
                        output = {"continue": True}
                    elif args.event == "preCompact":
                        output = {"user_message": "Project checkpoint saved. Context recovery is available at the next session start."}
                    else:
                        output = {}
                else:
                    output = {"sync": result, "context": context}
                print(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
            else:
                print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "list":
            for task in store.list_tasks(None if args.all else ["pending", "in_progress", "blocked"]):
                print(f"{task['id']} [{task['status']}] p{task['priority']} {task['description']}")
            return 0
        if args.command == "status":
            store.set_status(args.task_id, args.status, args.evidence); return 0
        if args.command == "checkpoint":
            print(store.checkpoint(args.summary, args.last, args.next)); return 0
        if args.command == "recover":
            print(json.dumps(store.recover(args.budget), indent=2, ensure_ascii=False)); return 0
        if args.command == "context":
            print(json.dumps(store.context_bundle(args.query, args.chars), indent=2, ensure_ascii=False)); return 0
        if args.command == "vault":
            project_vault = ProjectVault(store.path.parent / "vault")
            if args.vault_action == "path":
                print(project_vault.root); return 0
            if args.vault_action == "list":
                print(json.dumps(project_vault.list_notes(args.tag, args.limit), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "search":
                print(json.dumps(project_vault.search_notes(args.query, args.limit), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "read":
                print(json.dumps(project_vault.read_note(args.reference, args.chars), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "create":
                content = Path(args.content_file).expanduser().read_text(encoding="utf-8") if args.content_file else args.content
                print(json.dumps(project_vault.create_note(args.title, content, args.tag), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "append":
                print(json.dumps(project_vault.append_note(args.reference, args.content), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "link":
                print(json.dumps(project_vault.link_notes(args.source, args.target), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "links":
                print(json.dumps(project_vault.note_links(args.reference), indent=2, ensure_ascii=False)); return 0
            if args.vault_action == "backlinks":
                print(json.dumps(project_vault.backlinks(args.reference), indent=2, ensure_ascii=False)); return 0
        if args.command == "add":
            task_id, created = store.add_task(args.description, priority=args.priority, acceptance=args.accept, parent_id=args.parent)
            print(json.dumps({"id": task_id, "created": created})); return 0
        if args.command == "depends":
            store.set_dependencies(args.task_id, args.dependencies); return 0
        if args.command == "decision":
            store.add_decision(args.task_id, args.text); return 0
        if args.command == "files":
            store.add_relevant_files(args.task_id, args.paths); return 0
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
