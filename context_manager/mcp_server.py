"""Minimal MCP stdio server exposing the same persistent project store."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
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
    {"name": "search_notes", "description": "Search project vault notes by title, aliases, tags, properties, and Markdown content. Supports tag:#tag and [property:value] filters.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_note", "description": "Read a bounded excerpt of a vault note by ID, title, or vault-relative path.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}, "char_limit": {"type": "integer"}}, "required": ["reference"]}},
    {"name": "create_note", "description": "Create a local Markdown project note with Obsidian-compatible frontmatter, tags, aliases, and flat scalar properties.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string", "maxLength": 300}, "content": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 64}, "aliases": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 32}, "properties": {"type": "object", "maxProperties": 16, "propertyNames": {"type": "string", "pattern": "^(?![iI][dD]$|[tT][iI][tT][lL][eE]$|[cC][rR][eE][aA][tT][eE][dD]$|[uU][pP][dD][aA][tT][eE][dD]$|[tT][aA][gG][sS]$|[aA][lL][iI][aA][sS][eE][sS]$)[A-Za-z0-9_][A-Za-z0-9_. -]{0,79}$"}, "additionalProperties": {"oneOf": [{"type": ["string", "number", "boolean", "null"]}, {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}, "maxItems": 32}]}}}, "required": ["title"]}},
    {"name": "append_note", "description": "Append Markdown to a local project note without replacing its existing content.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}, "content": {"type": "string"}}, "required": ["reference", "content"]}},
    {"name": "link_notes", "description": "Add an Obsidian wikilink from one project note to another.", "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}}, "required": ["source", "target"]}},
    {"name": "note_links", "description": "List outgoing internal Markdown and Obsidian wikilinks from a project note, including unresolved links.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
    {"name": "note_backlinks", "description": "List project notes that link to a selected note using Markdown links or wikilinks.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
    {"name": "note_mentions", "description": "Find unlinked mentions of a note title or alias in other project notes.", "inputSchema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
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
MODERN_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION,)
SUPPORTED_PROTOCOL_VERSIONS = (*MODERN_PROTOCOL_VERSIONS, *LEGACY_PROTOCOL_VERSIONS)


def _write_error(request_id: str | int | None, code: int, message: str, data: dict | None = None) -> None:
    _write(_error_response(request_id, code, message, data))


def _error_response(request_id: str | int | None, code: int, message: str, data: dict | None = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


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


def _handle_request(request: object, root: str | Path | None, session: dict[str, Any], *, in_batch: bool = False) -> dict | None:
    modern_request = False
    try:
        if not isinstance(request, dict):
            return _error_response(None, -32600, "Invalid request")
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return _error_response(request_id, -32600, "Invalid request: jsonrpc must be '2.0'")
        if "id" in request and (
            request_id is None
            or isinstance(request_id, bool)
            or not isinstance(request_id, (str, int))
        ):
            return _error_response(None, -32600, "Invalid request: id must be a string or integer")
        method, params = request.get("method"), request.get("params", {})
        if not isinstance(method, str):
            return _error_response(request_id, -32600, "Invalid request: method must be a string")
        if not isinstance(params, dict):
            return _error_response(request_id, -32602, "Invalid params") if request_id is not None else None
        if "_meta" in params and not isinstance(params["_meta"], dict):
            return _error_response(request_id, -32602, "params._meta must be an object") if request_id is not None else None
        if in_batch and method == "initialize":
            return _error_response(request_id, -32600, "initialize must not be sent in a JSON-RPC batch")
        if in_batch and _is_modern_request(method, params):
            return _error_response(
                request_id,
                -32600,
                "Modern MCP requests must not be sent in a JSON-RPC batch",
            ) if request_id is not None else None
        if method == "notifications/initialized":
            return None
        if method in {"initialize", "tools/list", "tools/call", "server/discover"} and request_id is None:
            # These methods are requests with responses. In particular, do not
            # execute a state-changing tool call sent as a notification.
            return None

        modern_request = _is_modern_request(method, params)
        if modern_request:
            version, metadata_error = _validate_modern_meta(params)
            if metadata_error is not None:
                return _error_response(request_id, -32602, metadata_error) if request_id is not None else None
            if version != MODERN_PROTOCOL_VERSION:
                return _error_response(
                    request_id,
                    -32022,
                    "Unsupported protocol version",
                    {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version},
                ) if request_id is not None else None
            if method == "initialize":
                return _error_response(request_id, -32601, "Method not found")
        elif method != "initialize" and not session["legacy_initialized"]:
            return _error_response(
                request_id,
                -32602,
                "Request requires modern protocol metadata or a legacy initialize handshake",
            ) if request_id is not None else None

        if method == "initialize":
            if session["legacy_initialized"]:
                return _error_response(request_id, -32600, "initialize has already completed for this session")
            requested_version = params.get("protocolVersion")
            if not isinstance(requested_version, str):
                return _error_response(request_id, -32602, "initialize requires a string protocolVersion")
            protocol_version = (
                requested_version
                if requested_version in LEGACY_PROTOCOL_VERSIONS
                else LATEST_LEGACY_PROTOCOL_VERSION
            )
            result = {"protocolVersion": protocol_version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
            session["legacy_initialized"] = True
            session["legacy_protocol_version"] = protocol_version
        elif method == "server/discover":
            result = {
                # Discovery is a modern-era operation; legacy revisions use
                # the initialize handshake and are advertised in version errors.
                "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
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
                return _error_response(request_id, -32602, "Tool arguments must be an object")
            if not isinstance(name, str) or name not in _TOOL_NAMES:
                return _error_response(request_id, -32602, f"Unknown tool: {name}")
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
                elif name == "create_note": value = ProjectVault(store.path.parent / "vault").create_note(args["title"], args.get("content", ""), args.get("tags", []), args.get("aliases", []), args.get("properties", {}))
                elif name == "append_note": value = ProjectVault(store.path.parent / "vault").append_note(args["reference"], args["content"])
                elif name == "link_notes": value = ProjectVault(store.path.parent / "vault").link_notes(args["source"], args["target"])
                elif name == "note_links": value = ProjectVault(store.path.parent / "vault").note_links(args["reference"])
                elif name == "note_backlinks": value = ProjectVault(store.path.parent / "vault").backlinks(args["reference"])
                elif name == "note_mentions": value = ProjectVault(store.path.parent / "vault").mentions(args["reference"])
                elif name == "cleanup_project_state": value = store.cleanup()
                else: raise ValueError(f"Unknown tool: {name}")
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except Exception as error:
                # Tool execution errors are recoverable CallToolResult values.
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            finally:
                if store is not None:
                    store.close()
        else:
            return _error_response(request_id, -32601, "Method not found") if request_id is not None else None

        if request_id is None:
            return None
        if modern_request:
            if method == "tools/list":
                result["ttlMs"] = 300_000
                result["cacheScope"] = "public"
            elif method == "tools/call":
                result.setdefault("isError", False)
            result = _modern_response(result)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as error:
        if isinstance(request, dict) and "id" in request:
            request_id = request.get("id")
            if request_id is None or isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                request_id = None
            return _error_response(request_id, -32603 if modern_request else -32000, str(error))
        return None


_BATCH_PROTOCOL_VERSIONS = frozenset({"2025-03-26"})


def main(root: str | Path | None = None) -> None:
    if root is None:
        root = os.environ.get("CWM_PROJECT_ROOT")
    session: dict[str, Any] = {"legacy_initialized": False, "legacy_protocol_version": None}
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write_error(None, -32700, "Parse error")
            continue

        if isinstance(message, list):
            protocol_version = session["legacy_protocol_version"]
            if not message or protocol_version not in _BATCH_PROTOCOL_VERSIONS:
                _write_error(None, -32600, "Invalid request: JSON-RPC batches are not supported for this session")
                continue
            responses = [
                response
                for item in message
                if (response := _handle_request(item, root, session, in_batch=True)) is not None
            ]
            if responses:
                _write(responses)
            continue

        response = _handle_request(message, root, session)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main()
