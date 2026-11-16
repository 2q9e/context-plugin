"""Obsidian-compatible, project-local Markdown notes for durable context."""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .store import scrub


_FRONTMATTER = re.compile(r"\A---\r?\n(?P<header>.*?)\r?\n---(?:\r?\n|$)", re.S)
_WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
_TEMP_NOTE_WRITE = re.compile(r"^\..+\.md\.[0-9a-f]{32}\.tmp$")


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


def _link_text(text: str) -> str:
    """Return wiki links outside fenced and inline code."""
    visible: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        if fence_char:
            closing = re.match(rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*(?:\r?\n)?$", line)
            if closing:
                fence_char = None
                fence_length = 0
            visible.append(newline)
            continue

        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            marker = opening.group(1)
            # CommonMark forbids backticks in the info string of a backtick fence.
            if marker[0] == "`" and "`" in line[opening.end():]:
                visible.append(line)
                continue
            fence_char = marker[0]
            fence_length = len(marker)
            visible.append(newline)
            continue
        visible.append(line)

    text = "".join(visible)
    return re.sub(r"(?<!`)(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)", "", text, flags=re.S)


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
