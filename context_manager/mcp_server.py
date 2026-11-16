"""Minimal MCP stdio server exposing the same persistent project store."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from .store import project_store
from .vault import ProjectVault

TOOLS = [
    {"name": "sync_requirements", "description": "Append prompt requirements to this project's persistent checklist. Input is captured text; semantic interpretation remains the agent's responsibility.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "list_tasks", "description": "List pending, active, and blocked project tasks.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "set_task_status", "description": "Update a task. Completion requires structured verification evidence.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "cancelled"]}, "evidence": {"type": "string"}}, "required": ["task_id", "status"]}},
    {"name": "recover_context", "description": "Recover the latest checkpoint, active project tasks, and recent project notes.", "inputSchema": {"type": "object", "properties": {"budget": {"type": "integer"}}}},
    {"name": "search_context", "description": "Retrieve a project-local context bundle within a character budget.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "char_budget": {"type": "integer"}}, "required": ["query"]}},
    {"name": "checkpoint", "description": "Save a local recovery checkpoint.", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}, "last_action": {"type": "string"}, "next_action": {"type": "string"}}, "required": ["summary"]}},
    {"name": "list_notes", "description": "List Markdown notes in this project's Obsidian-compatible vault, optionally filtered by tag.", "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "search_notes", "description": "Search project vault notes by title, tags, and Markdown content.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_note", "description": "Read a bounded excerpt of a vault note by ID, title, or vault-relative path.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}, "char_limit": {"type": "integer"}}, "required": ["reference"]}},
    {"name": "create_note", "description": "Create a local Markdown project note with Obsidian-compatible frontmatter and tags.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title"]}},
    {"name": "append_note", "description": "Append Markdown to a local project note without replacing its existing content.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}, "content": {"type": "string"}}, "required": ["reference", "content"]}},
    {"name": "link_notes", "description": "Add an Obsidian wikilink from one project note to another.", "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}}, "required": ["source", "target"]}},
    {"name": "note_links", "description": "List outgoing Obsidian wikilinks from a project note, including unresolved links.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
    {"name": "note_backlinks", "description": "List project notes that link to a selected note.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
    {"name": "cleanup_project_state", "description": "Apply configured history retention, remove abandoned note-write temp files, and compact the project database while preserving notes and tasks.", "inputSchema": {"type": "object", "properties": {}}},
]
_TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
SERVER_INFO = {"name": "context-window-manager", "version": "0.1.0"}
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]
SUPPORTED_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)


def _write_error(request_id: str | int | None, code: int, message: str, data: dict | None = None) -> None:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _write({"jsonrpc": "2.0", "id": request_id, "error": error})


def _modern_response(result: dict) -> dict:
    response = dict(result)
    response["resultType"] = "complete"
    response["_meta"] = {SERVER_INFO_META_KEY: SERVER_INFO}
    return response


def _is_modern_request(method: str, params: dict) -> bool:
    if method == "server/discover":
        return True
    meta = params.get("_meta")
    return isinstance(meta, dict) and any(
        key in meta
        for key in (
            PROTOCOL_VERSION_META_KEY,
            CLIENT_CAPABILITIES_META_KEY,
            "io.modelcontextprotocol/clientInfo",
            "io.modelcontextprotocol/logLevel",
        )
    )


def _validate_modern_meta(params: dict) -> tuple[str | None, str | None]:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None, "Modern MCP requests require an object in params._meta"
    version = meta.get(PROTOCOL_VERSION_META_KEY)
    if not isinstance(version, str):
        return None, f"Modern MCP requests require string metadata {PROTOCOL_VERSION_META_KEY}"
    if not isinstance(meta.get(CLIENT_CAPABILITIES_META_KEY), dict):
        return None, f"Modern MCP requests require object metadata {CLIENT_CAPABILITIES_META_KEY}"
    return version, None


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(root: str | Path | None = None) -> None:
    if root is None:
        root = os.environ.get("CWM_PROJECT_ROOT")
    legacy_initialized = False
    for line in sys.stdin:
        request = None
        modern_request = False
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                _write_error(None, -32600, "Invalid request")
                continue
            request_id = request.get("id")
            if request.get("jsonrpc") != "2.0":
                _write_error(request_id, -32600, "Invalid request: jsonrpc must be '2.0'")
                continue
            if "id" in request and (
                request_id is None
                or isinstance(request_id, bool)
                or not isinstance(request_id, (str, int))
            ):
                _write_error(None, -32600, "Invalid request: id must be a string or integer")
                continue
            method, params, request_id = request.get("method"), request.get("params", {}), request.get("id")
            if not isinstance(method, str):
                _write_error(request_id, -32600, "Invalid request: method must be a string")
                continue
            if not isinstance(params, dict):
                if request_id is not None:
                    _write_error(request_id, -32602, "Invalid params")
                continue
            if "_meta" in params and not isinstance(params["_meta"], dict):
                if request_id is not None:
                    _write_error(request_id, -32602, "params._meta must be an object")
                continue
            if method == "notifications/initialized":
                continue
            if method in {"initialize", "tools/list", "tools/call", "server/discover"} and request_id is None:
                # These methods are requests with responses. In particular, do
                # not execute a state-changing tool call sent as a notification.
                continue
            modern_request = _is_modern_request(method, params)
            if modern_request:
                version, metadata_error = _validate_modern_meta(params)
                if metadata_error is not None:
                    if request_id is not None:
                        _write_error(request_id, -32602, metadata_error)
                    continue
                if version != MODERN_PROTOCOL_VERSION:
                    if request_id is not None:
                        _write_error(
                            request_id,
                            -32022,
                            "Unsupported protocol version",
                            {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version},
                        )
                    continue
                if method == "initialize":
                    _write_error(request_id, -32601, "Method not found")
                    continue
            elif method != "initialize" and not legacy_initialized:
                if request_id is not None:
                    _write_error(request_id, -32602, "Request requires modern protocol metadata or a legacy initialize handshake")
                continue
            if method == "initialize":
                requested_version = params.get("protocolVersion")
                if not isinstance(requested_version, str):
                    _write_error(request_id, -32602, "initialize requires a string protocolVersion")
                    continue
                protocol_version = (
                    requested_version
                    if requested_version in LEGACY_PROTOCOL_VERSIONS
                    else LATEST_LEGACY_PROTOCOL_VERSION
                )
                result = {"protocolVersion": protocol_version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
                legacy_initialized = True
            elif method == "server/discover":
                result = {
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {}},
                    "ttlMs": 300_000,
                    "cacheScope": "public",
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "ping" and not modern_request:
                result = {}
            elif method == "tools/call":
                args, name = params.get("arguments", {}), params.get("name")
                if not isinstance(args, dict):
                    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Tool arguments must be an object"}})
                    continue
                if not isinstance(name, str) or name not in _TOOL_NAMES:
                    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
                    continue
                store = None
                try:
                    store = project_store(root)
                    if name == "sync_requirements": value = store.sync_requirements(args["text"])
                    elif name == "list_tasks": value = store.list_tasks(["pending", "in_progress", "blocked"])
                    elif name == "set_task_status": store.set_status(args["task_id"], args["status"], args.get("evidence", "")); value = {"ok": True}
                    elif name == "recover_context": value = store.recover(args.get("budget", 12))
                    elif name == "search_context": value = store.context_bundle(args["query"], args.get("char_budget", 6000))
                    elif name == "checkpoint": value = {"id": store.checkpoint(args["summary"], args.get("last_action", ""), args.get("next_action", ""))}
                    elif name == "list_notes": value = ProjectVault(store.path.parent / "vault").list_notes(args.get("tag"), args.get("limit", 100))
                    elif name == "search_notes": value = ProjectVault(store.path.parent / "vault").search_notes(args["query"], args.get("limit", 8))
                    elif name == "read_note": value = ProjectVault(store.path.parent / "vault").read_note(args["reference"], args.get("char_limit", 20_000))
                    elif name == "create_note": value = ProjectVault(store.path.parent / "vault").create_note(args["title"], args.get("content", ""), args.get("tags", []))
                    elif name == "append_note": value = ProjectVault(store.path.parent / "vault").append_note(args["reference"], args["content"])
                    elif name == "link_notes": value = ProjectVault(store.path.parent / "vault").link_notes(args["source"], args["target"])
                    elif name == "note_links": value = ProjectVault(store.path.parent / "vault").note_links(args["reference"])
                    elif name == "note_backlinks": value = ProjectVault(store.path.parent / "vault").backlinks(args["reference"])
                    elif name == "cleanup_project_state": value = store.cleanup()
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
                if modern_request:
                    if method == "tools/list":
                        result["ttlMs"] = 300_000
                        result["cacheScope"] = "public"
                    elif method == "tools/call":
                        result.setdefault("isError", False)
                    result = _modern_response(result)
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:
            if isinstance(request, dict) and "id" in request:
                _write_error(request["id"], -32603 if modern_request else -32000, str(error))
            elif request is None:
                _write_error(None, -32700, "Parse error")


if __name__ == "__main__":
    main()
