"""Minimal MCP stdio server exposing the same persistent project store."""
from __future__ import annotations

import json
import sys
from .store import project_store

TOOLS = [
    {"name": "sync_requirements", "description": "Append prompt requirements to this project's persistent checklist. Input is captured text; semantic interpretation remains the agent's responsibility.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "list_tasks", "description": "List pending, active, and blocked project tasks.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "set_task_status", "description": "Update a task. Completion requires structured verification evidence.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["task_id", "status"]}},
    {"name": "recover_context", "description": "Recover the latest checkpoint and active project tasks.", "inputSchema": {"type": "object", "properties": {"budget": {"type": "integer"}}}},
    {"name": "search_context", "description": "Retrieve a project-local context bundle within a character budget.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "char_budget": {"type": "integer"}}, "required": ["query"]}},
    {"name": "checkpoint", "description": "Save a local recovery checkpoint.", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}, "last_action": {"type": "string"}, "next_action": {"type": "string"}}, "required": ["summary"]}},
]


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    root = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method, params, request_id = request.get("method"), request.get("params", {}), request.get("id")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "context-window-manager", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                args, name = params.get("arguments", {}), params.get("name")
                store = project_store(root)
                try:
                    if name == "sync_requirements": value = store.sync_requirements(args["text"])
                    elif name == "list_tasks": value = store.list_tasks(["pending", "in_progress", "blocked"])
                    elif name == "set_task_status": store.set_status(args["task_id"], args["status"], args.get("evidence", "")); value = {"ok": True}
                    elif name == "recover_context": value = store.recover(args.get("budget", 12))
                    elif name == "search_context": value = store.context_bundle(args["query"], args.get("char_budget", 6000))
                    elif name == "checkpoint": value = {"id": store.checkpoint(args["summary"], args.get("last_action", ""), args.get("next_action", ""))}
                    else: raise ValueError(f"Unknown tool: {name}")
                finally:
                    store.close()
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            else:
                continue
            if request_id is not None:
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:
            if isinstance(locals().get("request"), dict) and "id" in request:
                _write({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}})


if __name__ == "__main__":
    main()
