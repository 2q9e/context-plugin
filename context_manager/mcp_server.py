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
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
                continue
            method, params, request_id = request.get("method"), request.get("params", {}), request.get("id")
            if not isinstance(params, dict):
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid params"}})
                continue
            if method == "notifications/initialized":
                continue
            if method in {"initialize", "tools/list", "tools/call"} and request_id is None:
                # These methods are requests with responses. In particular, do
                # not execute a state-changing tool call sent as a notification.
                continue
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "context-window-manager", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                args, name = params.get("arguments", {}), params.get("name")
                store = None
                try:
                    if not isinstance(args, dict):
                        raise ValueError("Tool arguments must be an object")
                    store = project_store(root)
                    if name == "sync_requirements": value = store.sync_requirements(args["text"])
                    elif name == "list_tasks": value = store.list_tasks(["pending", "in_progress", "blocked"])
                    elif name == "set_task_status": store.set_status(args["task_id"], args["status"], args.get("evidence", "")); value = {"ok": True}
                    elif name == "recover_context": value = store.recover(args.get("budget", 12))
                    elif name == "search_context": value = store.context_bundle(args["query"], args.get("char_budget", 6000))
                    elif name == "checkpoint": value = {"id": store.checkpoint(args["summary"], args.get("last_action", ""), args.get("next_action", ""))}
                    else: raise ValueError(f"Unknown tool: {name}")
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                except Exception as error:
                    # Tool execution errors belong in CallToolResult so the model
                    # can see and recover from them; reserve JSON-RPC errors for
                    # malformed protocol requests and unknown methods.
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
                finally:
                    if store is not None:
                        store.close()
            else:
                if request_id is not None:
                    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                continue
            if request_id is not None:
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:
            if isinstance(request, dict) and "id" in request:
                _write({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(error)}})
            elif request is None:
                _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})


if __name__ == "__main__":
    main()
