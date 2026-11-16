"""Obsidian-compatible, project-local Markdown notes for durable context."""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import uuid
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .store import scrub


_FRONTMATTER = re.compile(r"\A---\r?\n(?P<header>.*?)\r?\n---(?:\r?\n|$)", re.S)
_WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
_TEMP_NOTE_WRITE = re.compile(r"^\..+\.md\.[0-9a-f]{32}\.tmp$")
_LIST_ITEM = re.compile(r"( {0,3})([-+*]|\d{1,9}[.)])([ \t]+|$)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quote(value: str) -> str:
    # JSON strings are valid YAML scalars and safely preserve arbitrary titles/tags.
    return json.dumps(value, ensure_ascii=False)


def _unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            result = json.loads(value)
            if isinstance(result, str):
                return result
        except json.JSONDecodeError:
            pass
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text, None

    header = match.group("header")
    properties: dict[str, Any] = {}
    tags: list[str] = []
    reading_tags = False
    for line in header.splitlines():
        item = re.match(r"^\s*-\s*(.*?)\s*$", line)
        if item and reading_tags:
            tag = _unquote(item.group(1))
            if tag:
                tags.append(tag)
            continue
        reading_tags = False
        key_value = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
        if not key_value:
            continue
        key, value = key_value.group(1).casefold(), key_value.group(2)
        if key == "tags":
            reading_tags = not value
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        tags.extend(str(tag) for tag in parsed if str(tag).strip())
                    else:
                        tags.extend(part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip())
                except json.JSONDecodeError:
                    tags.extend(part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip())
            elif value:
                tags.append(_unquote(value))
            properties["tags"] = tags
        elif value:
            properties[key] = _unquote(value)

    return properties, text[match.end():], header


def _strip_blockquotes(line: str) -> tuple[int, str]:
    depth = 0
    index = 0
    while index < len(line):
        start = index
        spaces = 0
        while index < len(line) and line[index] == " " and spaces < 3:
            index += 1
            spaces += 1
        if index >= len(line) or line[index] != ">":
            index = start
            break
        depth += 1
        index += 1
        if index < len(line) and line[index] in " \t":
            index += 1
    return depth, line[index:]


def _expand_tabs(line: str) -> str:
    """Expand tabs to CommonMark's four-column tab stops before block parsing."""
    expanded: list[str] = []
    column = 0
    for char in line:
        if char == "\t":
            spaces = 4 - column % 4
            expanded.append(" " * spaces)
            column += spaces
        else:
            expanded.append(char)
            column += 1
    return "".join(expanded)


def _columns(text: str, start_column: int = 0) -> int:
    column = start_column
    for char in text:
        if char == " ":
            column += 1
        elif char == "\t":
            column += 4 - column % 4
        else:
            break
    return column - start_column


def _remove_columns(text: str, columns: int) -> str | None:
    """Remove indentation columns, preserving any overshoot from a tab."""
    column = 0
    index = 0
    while index < len(text) and column < columns and text[index] in " \t":
        char = text[index]
        column += 1 if char == " " else 4 - column % 4
        index += 1
    if column < columns:
        return None
    return " " * (column - columns) + text[index:]


def _strip_fence_prefixes(line: str, prefixes: list[tuple[str, int]]) -> str | None:
    """Remove an opening fence's blockquote/list container prefixes in order."""
    index = 0
    for prefix_index, (kind, amount) in enumerate(prefixes):
        if kind == "list":
            consumed = 0
            while index < len(line) and line[index] == " " and consumed < amount:
                index += 1
                consumed += 1
            if consumed < amount:
                if line[index:].strip() or any(
                    later_kind == "quote" for later_kind, _ in prefixes[prefix_index + 1:]
                ):
                    return None
                return ""
            continue

        start = index
        spaces = 0
        while index < len(line) and line[index] == " " and spaces < 3:
            index += 1
            spaces += 1
        if index >= len(line) or line[index] != ">":
            return None
        index += 1
        if index < len(line) and line[index] == " ":
            index += 1
        # Keep the no-progress case explicit if a future prefix kind is added.
        if index == start:
            return None
    return line[index:]


def _strip_list_items(line: str) -> tuple[str, int, int, list[int]]:
    """Strip list markers and return their combined content indentation."""
    index = 0
    content_indent = 0
    count = 0
    indents: list[int] = []
    while match := _LIST_ITEM.match(line, index):
        leading, marker, padding = match.groups()
        marker_column = content_indent + _columns(leading) + len(marker)
        padding_columns = _columns(padding, marker_column) if padding else 0
        padding_chars = len(padding)
        if padding_columns > 4:
            padding_columns = 1
            padding_chars = 1
        index += len(leading) + len(marker) + padding_chars
        content_indent = marker_column + padding_columns
        indents.append(content_indent)
        count += 1
        if not padding:
            break
    return line[index:], content_indent, count, indents


def _fit_list_context(text: str, indents: list[int]) -> tuple[int, str] | None:
    available = _columns(text)
    match = bisect_right(indents, available) - 1
    if match < 0:
        return None
    indent = indents[match]
    stripped = _remove_columns(text, indent)
    return (indent, stripped) if stripped is not None else None


def _remember_list_context(contexts: dict[int, list[int]], quote_depth: int, indent: int) -> None:
    if indent <= 0:
        return
    levels = contexts.setdefault(quote_depth, [])
    if not levels or indent > levels[-1]:
        levels.append(indent)
    elif indent < levels[-1]:
        del levels[bisect_left(levels, indent):]
        levels.append(indent)


def _mask_inline_code(paragraph: str) -> str:
    """Mask CommonMark code spans in linear time, honoring escaped openers."""
    runs: list[tuple[int, int, bool]] = []
    index = 0
    while index < len(paragraph):
        if paragraph[index] != "`":
            index += 1
            continue
        end = index + 1
        while end < len(paragraph) and paragraph[end] == "`":
            end += 1
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and paragraph[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        runs.append((index, end, bool(backslashes % 2)))
        index = end

    next_same_length: dict[int, int] = {}
    last_by_length: dict[int, int] = {}
    for run_index, (start, end, _) in enumerate(runs):
        length = end - start
        previous = last_by_length.get(length)
        if previous is not None:
            next_same_length[previous] = run_index
        last_by_length[length] = run_index

    visible = list(paragraph)
    run_index = 0
    while run_index < len(runs):
        start, end, escaped = runs[run_index]
        closer = next_same_length.get(run_index)
        if escaped or closer is None:
            run_index += 1
            continue
        close_end = runs[closer][1]
        for position in range(start, close_end):
            if visible[position] != "\n":
                visible[position] = " "
        run_index = closer + 1
    return "".join(visible)


def _link_text(text: str) -> str:
    """Return wiki links outside CommonMark fenced, indented, and inline code."""
    # CommonMark accepts LF, CRLF, and bare CR as line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    visible: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    fence_prefixes: list[tuple[str, int]] = []
    fence_quote_depth = 0
    paragraph_open = False
    previous_quote_depth: int | None = None
    list_contexts: dict[int, list[int]] = {}

    lines = text.split("\n")
    for line_index, line in enumerate(lines):
        newline = "\n" if line_index < len(lines) - 1 else ""
        source = _expand_tabs(line)
        # A top-level list's continuation indentation comes before any nested
        # blockquote marker. Lists inside blockquotes take the opposite order:
        # strip the enclosing quote first, then the list indentation.
        prequote_list_indent = 0
        quote_source = source
        root_context = _fit_list_context(source, list_contexts.get(0, []))
        if root_context is not None:
            prequote_list_indent, quote_source = root_context
            root_levels = list_contexts[0]
            del root_levels[bisect_right(root_levels, prequote_list_indent):]
        quote_depth, quoted = _strip_blockquotes(quote_source)

        if fence_char:
            # Reapply each containing block prefix in its original order. A
            # list can contain a quote (or vice versa), so testing all quote
            # markers before all list indentation loses valid fence closers.
            fence_content = _strip_fence_prefixes(source, fence_prefixes)
            if fence_content is None:
                fence_char = None
                fence_length = 0
                paragraph_open = False
            else:
                closing = re.fullmatch(
                    rf" {{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*",
                    fence_content,
                )
                if closing:
                    fence_char = None
                    fence_length = 0
                    paragraph_open = False
                visible.append(newline)
                previous_quote_depth = fence_quote_depth
                continue

        if quote_depth > 0 and not prequote_list_indent:
            # An unindented quote is outside a top-level list container.
            list_contexts.pop(0, None)
        if previous_quote_depth is not None and quote_depth < previous_quote_depth and not paragraph_open:
            for depth in tuple(list_contexts):
                if depth > quote_depth:
                    list_contexts.pop(depth, None)

        list_input = quoted
        postquote_list_indent = 0
        quote_context = _fit_list_context(quoted, list_contexts.get(quote_depth, []))
        if quote_context is not None and not (prequote_list_indent and quote_depth == 0):
            postquote_list_indent, list_input = quote_context
            quote_levels = list_contexts[quote_depth]
            del quote_levels[bisect_right(quote_levels, postquote_list_indent):]
        elif list_contexts.get(quote_depth) and not paragraph_open and quoted.strip():
            list_contexts.pop(quote_depth, None)

        list_content, local_list_indent, list_count, local_list_indents = _strip_list_items(list_input)
        opening_prefixes: list[tuple[str, int]] = []
        if prequote_list_indent:
            opening_prefixes.append(("list", prequote_list_indent))
        opening_prefixes.extend([("quote", 1)] * quote_depth)
        if postquote_list_indent:
            opening_prefixes.append(("list", postquote_list_indent))
        if list_count:
            opening_prefixes.append(("list", local_list_indent))

        list_context_updates: dict[int, list[int]] = {}
        if prequote_list_indent:
            list_context_updates[0] = (
                [prequote_list_indent + indent for indent in local_list_indents]
                if list_count and quote_depth == 0 else [prequote_list_indent]
            )
        if quote_depth > 0 and (postquote_list_indent or list_count):
            list_context_updates[quote_depth] = (
                [postquote_list_indent + indent for indent in local_list_indents]
                if list_count else [postquote_list_indent]
            )
        elif quote_depth == 0 and not prequote_list_indent and list_count:
            list_context_updates[0] = local_list_indents

        # Containers can alternate on a single line, for example a list item
        # containing a quote containing another list item. Record their order
        # so later lines must carry the same prefixes to remain in the fence.
        while True:
            nested_quote_depth, nested_content = _strip_blockquotes(list_content)
            if nested_quote_depth:
                opening_prefixes.extend([("quote", 1)] * nested_quote_depth)
                quote_depth += nested_quote_depth
                list_content = nested_content
                nested_context = _fit_list_context(list_content, list_contexts.get(quote_depth, []))
                if nested_context is not None:
                    nested_indent, list_content = nested_context
                    opening_prefixes.append(("list", nested_indent))
                    list_context_updates[quote_depth] = [nested_indent]
            nested_list_content, nested_list_indent, nested_list_count, nested_list_indents = _strip_list_items(list_content)
            if not nested_list_count:
                break
            opening_prefixes.append(("list", nested_list_indent))
            list_content = nested_list_content
            list_count += nested_list_count
            nested_context_base = list_context_updates.get(quote_depth, [])
            base_indent = nested_context_base[-1] if nested_context_base else 0
            list_context_updates[quote_depth] = [base_indent + indent for indent in nested_list_indents]
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", list_content)
        if opening:
            marker = opening.group(1)
            # Backtick fences cannot have a backtick in their info string.
            if marker[0] != "`" or "`" not in opening.group(2):
                fence_char = marker[0]
                fence_length = len(marker)
                fence_prefixes = opening_prefixes
                fence_quote_depth = sum(kind == "quote" for kind, _ in opening_prefixes)
                for context_depth, indents in list_context_updates.items():
                    for indent in indents:
                        _remember_list_context(list_contexts, context_depth, indent)
                visible.append(newline)
                paragraph_open = False
                previous_quote_depth = quote_depth
                continue

        blank = not list_content.strip()
        if blank:
            visible.append(newline)
            paragraph_open = False
            for depth in tuple(list_contexts):
                if depth > quote_depth:
                    list_contexts.pop(depth, None)
            previous_quote_depth = quote_depth
            continue

        # A list marker or a new block quote starts a new block. An unmarked
        # line may lazily continue a paragraph in an open blockquote.
        if list_count:
            paragraph_open = False
            for context_depth, indents in list_context_updates.items():
                for indent in indents:
                    _remember_list_context(list_contexts, context_depth, indent)
        heading = bool(re.match(r"^ {0,3}#{1,6}(?:[ \t]|$)", list_content))
        thematic_break = bool(re.match(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$", list_content))
        html_block = bool(re.match(r"^ {0,3}<!--|^ {0,3}</?[A-Za-z][^>]*>", list_content))
        quote_changed = previous_quote_depth is not None and quote_depth != previous_quote_depth
        lazy_quote_continuation = (
            quote_changed
            and previous_quote_depth is not None
            and quote_depth < previous_quote_depth
            and paragraph_open
            and not list_count
            and not heading
            and not thematic_break
            and not html_block
        )
        if quote_changed and not lazy_quote_continuation:
            paragraph_open = False
            for depth in tuple(list_contexts):
                if depth > quote_depth:
                    list_contexts.pop(depth, None)
        if not paragraph_open and _columns(list_content) >= 4:
            visible.append(newline)
            paragraph_open = False
            previous_quote_depth = quote_depth
            continue

        visible.append(line + newline)
        paragraph_open = not (heading or thematic_break or html_block)
        previous_quote_depth = quote_depth

    # A blank line or a block boundary prevents inline delimiters in separate
    # paragraphs from pairing. Mask each remaining paragraph independently.
    masked: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            masked.append(_mask_inline_code("".join(paragraph)))
            paragraph.clear()

    quote_context: int | None = None
    list_context = 0
    for line in visible:
        source = _expand_tabs(line[:-1] if line.endswith("\n") else line)
        quote_depth, quoted = _strip_blockquotes(source)
        list_content, list_indent, list_count, _ = _strip_list_items(quoted)
        nested_quote_depth, nested_content = _strip_blockquotes(list_content)
        if nested_quote_depth:
            quote_depth += nested_quote_depth
            list_content = nested_content
            list_indent = 0
            nested_list_content, nested_list_indent, nested_list_count, _ = _strip_list_items(list_content)
            if nested_list_count:
                list_content = nested_list_content
                list_indent = nested_list_indent
                list_count += nested_list_count
        if not source.strip():
            flush_paragraph()
            masked.append(line)
            quote_context = None
            list_context = 0
            continue
        heading = bool(re.match(r"^ {0,3}#{1,6}(?:[ \t]|$)", list_content))
        thematic_break = bool(re.match(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$", list_content))
        html_block = bool(re.match(r"^ {0,3}<!--|^ {0,3}</?[A-Za-z][^>]*>", list_content))
        quote_change = quote_context is not None and quote_depth != quote_context
        block_start = list_count or heading or thematic_break or html_block or (
            quote_context is not None and quote_depth > quote_context
        )
        lazy_continuation = bool(paragraph) and not block_start and (
            list_context > 0 or (quote_context is not None and quote_depth < quote_context)
        )
        if quote_change and not lazy_continuation:
            flush_paragraph()
            list_context = 0
        if heading or thematic_break:
            flush_paragraph()
            masked.append(_mask_inline_code(line))
            flush_paragraph()
            quote_context = quote_depth
            list_context = 0
            continue
        if list_count:
            flush_paragraph()
            list_context = list_indent
        elif list_context and _columns(quoted) < list_context and not lazy_continuation:
            flush_paragraph()
            list_context = 0
        paragraph.append(line)
        quote_context = quote_depth
    flush_paragraph()
    return "".join(masked)


def _link_target(value: str) -> str:
    target = value.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/")
    target = unquote(target)
    if target.casefold().endswith(".md"):
        target = target[:-3]
    return target.casefold().strip(" /")


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    result = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")[:72].rstrip("-")
    return result or f"note-{uuid.uuid4().hex[:10]}"


def _excerpt(text: str, terms: set[str], limit: int = 280) -> str:
    plain = re.sub(r"\s+", " ", text).strip()
    folded = plain.casefold()
    positions = [folded.find(term) for term in terms if term and folded.find(term) >= 0]
    start = max(0, (min(positions) - limit // 4)) if positions else 0
    excerpt = plain[start:start + limit]
    if start:
        excerpt = "…" + excerpt
    if start + limit < len(plain):
        excerpt += "…"
    return excerpt


class ProjectVault:
    """A small file-backed notes layer that Obsidian can open as a vault.

    Markdown files are the source of truth for notes. Task state remains in the
    SQLite store; this class only adds searchable project knowledge and links.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()
        self._real_root = self.root.resolve()

    def _files(self) -> list[Path]:
        if not self.root.is_dir() or self.root.is_symlink():
            return []
        result: list[Path] = []
        for path in self.root.rglob("*.md"):
            relative = path.relative_to(self.root)
            if path.is_symlink() or any(part.startswith(".") for part in relative.parts):
                continue
            try:
                if path.is_file() and path.resolve().is_relative_to(self._real_root):
                    result.append(path)
            except OSError:
                continue
        return sorted(result, key=lambda item: item.relative_to(self.root).as_posix().casefold())

    def _document(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        properties, body, _ = _parse_frontmatter(text)
        relative = path.relative_to(self.root).as_posix()
        first_heading = re.search(r"(?m)^#\s+(.+?)\s*#*\s*$", body)
        title = scrub(str(properties.get("title") or (first_heading.group(1) if first_heading else path.stem))).strip()
        tags = properties.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)] if tags else []
        tags = [scrub(str(tag)).lstrip("#") for tag in tags if str(tag).strip()]
        safe_body = scrub(body)
        links = [match.group(1).strip() for match in _WIKILINK.finditer(_link_text(safe_body))]
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except OSError:
            modified = ""
        return {
            "id": scrub(str(properties.get("id") or Path(relative).with_suffix("").as_posix())),
            "title": title,
            "path": relative,
            "tags": [str(tag).lstrip("#") for tag in tags if str(tag).strip()],
            "created": scrub(str(properties.get("created") or "")),
            "updated": scrub(str(properties.get("updated") or modified)),
            "content": safe_body,
        }

    def _documents(self) -> list[dict[str, Any]]:
        return [doc for path in self._files() if (doc := self._document(path)) is not None]

    def cleanup(self, older_than_seconds: int = 86_400) -> dict[str, int]:
        """Remove abandoned note-rewrite temp files older than one day."""
        result = {"removed_temp_files": 0, "failed_temp_files": 0}
        if not self.root.is_dir() or self.root.is_symlink():
            return result
        cutoff = time.time() - max(86_400, older_than_seconds)
        for path in self.root.rglob("*"):
            try:
                relative = path.relative_to(self.root)
                if any(part.startswith(".") for part in relative.parts[:-1]):
                    continue
                if path.is_symlink() or not path.is_file() or not _TEMP_NOTE_WRITE.fullmatch(path.name):
                    continue
                if not path.resolve().is_relative_to(self._real_root) or path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                result["removed_temp_files"] += 1
            except FileNotFoundError:
                continue
            except OSError:
                result["failed_temp_files"] += 1
        return result

    @staticmethod
    def _keys(doc: dict[str, Any]) -> set[str]:
        path = str(doc["path"])
        no_suffix = Path(path).with_suffix("").as_posix().casefold()
        return {
            str(doc["id"]).casefold(),
            no_suffix,
            Path(no_suffix).name,
            str(doc["title"]).casefold().strip(),
        }

    @staticmethod
    def _key_index(documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Index note aliases once when resolving several links in one operation."""
        by_key: dict[str, list[dict[str, Any]]] = {}
        for doc in documents:
            for key in ProjectVault._keys(doc):
                by_key.setdefault(key, []).append(doc)
        return by_key

    @staticmethod
    def _reference_key(reference: str) -> str:
        key = _link_target(reference)
        if not key or key.startswith("."):
            raise ValueError("Note reference must be a note ID, title, or vault-relative path")
        return key

    @staticmethod
    def _resolve_candidates(reference: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
        if not matches:
            raise KeyError(f"Unknown note: {reference}")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous note reference: {reference}; use a note ID or full vault-relative path")
        return matches[0]

    @staticmethod
    def _resolve_from_index(reference: str, by_key: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        key = ProjectVault._reference_key(reference)
        return ProjectVault._resolve_candidates(reference, by_key.get(key, []))

    @staticmethod
    def _resolve_from_documents(reference: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
        key = ProjectVault._reference_key(reference)
        matches = [doc for doc in documents if key in ProjectVault._keys(doc)]
        return ProjectVault._resolve_candidates(reference, matches)

    def _resolve(self, reference: str) -> dict[str, Any]:
        key = self._reference_key(reference)
        documents = self._documents()
        matches = [doc for doc in documents if key in self._keys(doc)]
        return self._resolve_candidates(reference, matches)

    @staticmethod
    def _summary(doc: dict[str, Any]) -> dict[str, Any]:
        return {key: doc[key] for key in ("id", "title", "path", "tags", "created", "updated")}

    def list_notes(self, tag: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        documents = self._documents()
        if tag:
            wanted = tag.lstrip("#").casefold()
            documents = [doc for doc in documents if any(value.casefold() == wanted for value in doc["tags"])]
        documents.sort(key=lambda doc: (doc["updated"], doc["title"].casefold()), reverse=True)
        return [self._summary(doc) for doc in documents[:max(0, min(limit, 1000))]]

    def recent_notes(self, limit: int = 5) -> list[dict[str, Any]]:
        documents = self._documents()
        documents.sort(key=lambda doc: (doc["updated"], doc["title"].casefold()), reverse=True)
        result = []
        for doc in documents[:max(0, min(limit, 100))]:
            item = self._summary(doc)
            item["excerpt"] = _excerpt(doc["content"], set())
            item["links"] = [link.strip() for link in _WIKILINK.findall(_link_text(doc["content"]))]
            result.append(item)
        return result

    def read_note(self, reference: str, char_limit: int = 20_000) -> dict[str, Any]:
        doc = self._resolve(reference)
        char_limit = max(256, min(char_limit, 100_000))
        result = self._summary(doc)
        result["content"] = doc["content"][:char_limit]
        result["total_chars"] = len(doc["content"])
        result["truncated"] = len(doc["content"]) > char_limit
        result["links"] = [link.strip() for link in _WIKILINK.findall(_link_text(doc["content"]))]
        return result

    def search_notes(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        terms = set(re.findall(r"[\w-]+", query.casefold()))
        if not terms:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for doc in self._documents():
            title = doc["title"].casefold()
            tags = " ".join(doc["tags"]).casefold()
            content = doc["content"].casefold()
            score = sum(5 for term in terms if term in title)
            score += sum(3 for term in terms if term in tags)
            score += sum(1 for term in terms if term in content)
            if score:
                item = self._summary(doc)
                item["score"] = score
                item["excerpt"] = _excerpt(doc["content"], terms)
                item["links"] = [link.strip() for link in _WIKILINK.findall(_link_text(doc["content"]))]
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1]["updated"], pair[1]["title"].casefold()), reverse=True)
        return [item for _, item in scored[:max(0, min(limit, 100))]]

    def note_links(self, reference: str) -> list[dict[str, Any]]:
        documents = self._documents()
        by_key = self._key_index(documents)
        doc = self._resolve_from_index(reference, by_key)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in _WIKILINK.findall(_link_text(doc["content"])):
            target = raw.strip()
            normalized = _link_target(target)
            candidates = by_key.get(normalized, [])
            resolved = candidates[0] if len(candidates) == 1 else None
            identity = resolved["id"] if resolved else normalized
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"target": target, "resolved": self._summary(resolved) if resolved else None,
                           "ambiguous": len(candidates) > 1})
        return result

    def backlinks(self, reference: str) -> list[dict[str, Any]]:
        documents = self._documents()
        target = self._resolve_from_documents(reference, documents)
        target_keys = self._keys(target)
        results = []
        for source in documents:
            if source["id"] == target["id"]:
                continue
            for raw in _WIKILINK.findall(_link_text(source["content"])):
                if _link_target(raw) in target_keys:
                    item = self._summary(source)
                    item["excerpt"] = _excerpt(source["content"], {_link_target(raw)})
                    results.append(item)
                    break
        return results

    def create_note(self, title: str, content: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        safe_title = re.sub(r"\s+", " ", scrub(title).strip())
        if not safe_title:
            raise ValueError("Note title cannot be empty")
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)):
            raise ValueError("Tags must be a list of strings")
        clean_tags: list[str] = []
        for tag in tags or []:
            value = scrub(str(tag)).strip().lstrip("#").casefold()
            if not value or not re.fullmatch(r"[\w/-]+", value, flags=re.UNICODE):
                raise ValueError(f"Invalid tag: {tag}")
            if value not in clean_tags:
                clean_tags.append(value)
        if any(doc["title"].casefold() == safe_title.casefold() for doc in self._documents()):
            raise ValueError(f"A note titled '{safe_title}' already exists")

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("The project vault directory cannot be a symbolic link")
        stem = _slug(safe_title)
        path = self.root / f"{stem}.md"
        suffix = 2
        while path.exists():
            path = self.root / f"{stem}-{suffix}.md"
            suffix += 1
        created = _now()
        note_id = f"note-{uuid.uuid4().hex[:12]}"
        frontmatter = ["---", f"id: {_quote(note_id)}", f"title: {_quote(safe_title)}",
                       f"created: {_quote(created)}", f"updated: {_quote(created)}"]
        if clean_tags:
            frontmatter.append("tags:")
            frontmatter.extend(f"  - {_quote(tag)}" for tag in clean_tags)
        frontmatter.append("---")
        body = scrub(content).strip()
        markdown = "\n".join(frontmatter) + f"\n\n# {safe_title}\n"
        if body:
            markdown += f"\n{body}\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(markdown)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self._summary(self._document(path) or {"id": note_id, "title": safe_title, "path": path.name,
                                                       "tags": clean_tags, "created": created, "updated": created})

    def _replace_body(self, doc: dict[str, Any], body: str) -> None:
        path = self.root / doc["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(self._real_root):
            raise ValueError("Refusing to update a note outside the project vault")
        original = path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(original)
        updated = _quote(_now())
        if match:
            header = match.group("header")
            if re.search(r"(?m)^updated\s*:", header):
                header = re.sub(r"(?m)^updated\s*:\s*.*$", f"updated: {updated}", header, count=1)
            else:
                header += f"\nupdated: {updated}"
            replacement = f"---\n{header}\n---\n\n{body.rstrip()}\n"
        else:
            properties, _, _ = _parse_frontmatter(original)
            created = str(properties.get("created") or _now())
            tags = properties.get("tags", [])
            if not isinstance(tags, list):
                tags = [str(tags)] if tags else []
            header = ["---", f"id: {_quote(str(properties.get('id') or doc['id']))}",
                      f"title: {_quote(str(properties.get('title') or doc['title']))}",
                      f"created: {_quote(created)}", f"updated: {updated}"]
            if tags:
                header.append("tags:")
                header.extend(f"  - {_quote(str(tag))}" for tag in tags)
            header.append("---")
            replacement = "\n".join(header) + f"\n\n{body.rstrip()}\n"

        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_text(encoding="utf-8") != original:
                raise RuntimeError("Note changed while it was being updated; retry the operation")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def append_note(self, reference: str, content: str) -> dict[str, Any]:
        addition = scrub(content).strip()
        if not addition:
            raise ValueError("Note content cannot be empty")
        doc = self._resolve(reference)
        updated_body = doc["content"].rstrip() + "\n\n" + addition + "\n"
        self._replace_body(doc, updated_body)
        return self._summary(self._resolve(doc["id"]))

    def link_notes(self, source: str, target: str) -> dict[str, Any]:
        documents = self._documents()
        by_key = self._key_index(documents)
        source_doc = self._resolve_from_index(source, by_key)
        target_doc = self._resolve_from_index(target, by_key)
        if source_doc["id"] == target_doc["id"]:
            raise ValueError("A note cannot be linked to itself")
        target_path = Path(target_doc["path"]).with_suffix("").as_posix()
        link = f"[[{target_path}]]"
        body = source_doc["content"]
        for raw in _WIKILINK.findall(_link_text(body)):
            try:
                if self._resolve_from_index(raw, by_key)["id"] == target_doc["id"]:
                    return {"source": self._summary(source_doc), "target": self._summary(target_doc), "created": False}
            except (KeyError, ValueError):
                continue
        section = "## Related notes"
        if section.casefold() in body.casefold():
            offset = body.casefold().find(section.casefold()) + len(section)
            body = body[:offset].rstrip() + f"\n- {link}" + body[offset:]
        else:
            body = body.rstrip() + f"\n\n{section}\n\n- {link}\n"
        self._replace_body(source_doc, body)
        updated_by_key = self._key_index(self._documents())
        return {"source": self._summary(self._resolve_from_index(source_doc["id"], updated_by_key)),
                "target": self._summary(self._resolve_from_index(target_doc["id"], updated_by_key)), "created": True}
