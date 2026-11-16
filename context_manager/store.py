"""Transactional SQLite persistence for requirements, tasks, and checkpoints."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str) -> str:
    value = re.sub(r"\W+", " ", text.casefold()).strip()
    return re.sub(r"^(?:please )?(?:also )?(?:add|implement|build|create|support|include|provide)\s+", "", value)


def split_requirements(text: str) -> list[str]:
    """Conservatively split prompt prose into actionable clauses."""
    clauses = re.split(r"\n+|(?<=[.!?])\s+|;\s+|\s+\b(?:also|additionally)\b\s+", text.strip(), flags=re.I)
    result: list[str] = []
    for item in clauses:
        item = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", item).strip()
        item = re.sub(r"^(?:also|additionally)\s+", "", item, flags=re.I)
        item = item.strip(" \t\r\n.,;:")
        if len(item) >= 4 and normalize(item) not in {normalize(x) for x in result}:
            result.append(item)
    return result


_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+[^\s,;]+"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|auth[_-]?(?:token|key)|client[_-]?secret|private[_-]?key|secret(?:[_-]?access[_-]?key|[_-]?key)?|password|passwd|token|credential|database[_-]?url|db[_-]?url|connection[_-]?string|cookie|session[_-]?(?:id|token|key)))\s*([=:])\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?(?:-----END (?P=label)-----|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)


def scrub(text: str) -> str:
    """Redact common credential formats before they enter persistent storage."""
    result = _PRIVATE_KEY_BLOCK.sub("[REDACTED]", text)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: (m.group(1) + "=" if m.lastindex and m.group(1) else "") + "[REDACTED]", result)
    return result


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        state_dir = self.path.parent
        if state_dir.is_symlink() or self.path.is_symlink():
            raise ValueError("Refusing to open a symlinked project state path")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(f"{self.path}{suffix}").is_symlink():
                raise ValueError("Refusing to open a symlinked SQLite sidecar")
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state_dir.is_symlink() or self.path.is_symlink():
            raise ValueError("Refusing to open a symlinked project state path")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("The project state database path is not a regular file")
        if os.name == "posix":
            os.chmod(state_dir, 0o700)
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self._restrict_sqlite_permissions()
        self.db.row_factory = sqlite3.Row
        self._savepoint_counter = 0
        self.db.execute("PRAGMA journal_mode=WAL")
        self._restrict_sqlite_permissions()
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=15000")
        self._schema()
        self._history_prune_error = None
        try:
            self._pruned_on_open = self._prune_history()
        except sqlite3.Error as error:
            # Opening a project should still work when another process briefly
            # holds the database; cleanup can report and retry the retention pass.
            self._pruned_on_open = {"events": 0, "checkpoints": 0}
            self._history_prune_error = str(error)
        self._restrict_sqlite_permissions()

    def _restrict_sqlite_permissions(self) -> None:
        """Keep SQLite state private on POSIX, including WAL and SHM sidecars."""
        if os.name != "posix":
            return
        os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.is_symlink():
                raise ValueError("Refusing to use a symlinked SQLite sidecar")
            if sidecar.exists():
                os.chmod(sidecar, 0o600)

    def close(self) -> None:
        self.db.close()

    def _schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, description TEXT NOT NULL, normalized TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','in_progress','blocked','completed','cancelled')),
          priority INTEGER NOT NULL DEFAULT 2, dependencies TEXT NOT NULL DEFAULT '[]',
          parent_id TEXT REFERENCES tasks(id), acceptance TEXT NOT NULL DEFAULT '[]',
          relevant_files TEXT NOT NULL DEFAULT '[]', decisions TEXT NOT NULL DEFAULT '[]',
          verification TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id),
          kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
          id TEXT PRIMARY KEY, summary TEXT NOT NULL, last_action TEXT NOT NULL,
          next_action TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status, priority, updated_at);
        CREATE INDEX IF NOT EXISTS tasks_active_normalized_idx ON tasks(normalized) WHERE status!='cancelled';
        CREATE INDEX IF NOT EXISTS events_task_idx ON events(task_id, id);
        CREATE INDEX IF NOT EXISTS events_created_at_idx ON events(created_at);
        CREATE INDEX IF NOT EXISTS checkpoints_created_at_idx ON checkpoints(created_at);
        """)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Make each multi-statement mutation atomic, including inside sync transactions."""
        if self.db.in_transaction:
            self._savepoint_counter += 1
            savepoint = f"cwm_write_{self._savepoint_counter}"
            self.db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return

        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _prune_history(self) -> dict[str, int]:
        """Optional retention applies to history only; active requirements remain durable."""
        removed = {"events": 0, "checkpoints": 0}
        try:
            days = int(os.environ.get("CWM_RETENTION_DAYS", "0"))
        except ValueError:
            days = 0
        if days > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            cutoff_text = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
            with self._write_transaction():
                removed["events"] = max(0, self.db.execute("DELETE FROM events WHERE created_at < ?", (cutoff_text,)).rowcount)
                removed["checkpoints"] = max(0, self.db.execute("DELETE FROM checkpoints WHERE created_at < ?", (cutoff_text,)).rowcount)
        return removed

    def cleanup(self) -> dict[str, Any]:
        """Apply history retention, remove abandoned note temp files, and compact SQLite."""
        history_error = self._history_prune_error
        try:
            pruned_now = self._prune_history()
        except sqlite3.Error as error:
            pruned_now = {"events": 0, "checkpoints": 0}
            history_error = str(error)
        removed_on_open = getattr(self, "_pruned_on_open", {"events": 0, "checkpoints": 0})
        result: dict[str, Any] = {
            "expired_events_removed": removed_on_open["events"] + pruned_now["events"],
            "expired_checkpoints_removed": removed_on_open["checkpoints"] + pruned_now["checkpoints"],
            "history_cleanup_error": history_error,
            "stale_note_temp_files_removed": 0,
            "note_temp_cleanup_failures": 0,
            "sqlite_compacted": False,
            "sqlite_cleanup_error": None,
        }
        self._pruned_on_open = {"events": 0, "checkpoints": 0}
        self._history_prune_error = None

        from .vault import ProjectVault

        notes = ProjectVault(self.path.parent / "vault").cleanup()
        result["stale_note_temp_files_removed"] = notes["removed_temp_files"]
        result["note_temp_cleanup_failures"] = notes["failed_temp_files"]

        try:
            checkpoint = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]):
                result["sqlite_cleanup_error"] = "WAL checkpoint is busy; SQLite vacuum was skipped"
                return result
            self.db.execute("VACUUM")
            result["sqlite_compacted"] = True
        except sqlite3.Error as error:
            result["sqlite_cleanup_error"] = str(error)
        return result

    def _event(self, task_id: str | None, kind: str, detail: str) -> None:
        self.db.execute("INSERT INTO events(task_id,kind,detail,created_at) VALUES(?,?,?,?)", (task_id, kind, detail, now()))

    def add_task(self, description: str, *, priority: int = 2, acceptance: Iterable[str] = (), parent_id: str | None = None) -> tuple[str, bool]:
        description = description.strip()
        if not description:
            raise ValueError("Task description cannot be empty")
        description = scrub(description)
        key = normalize(description)
        criteria: list[str] = []
        for criterion in acceptance:
            if not isinstance(criterion, str) or not criterion.strip():
                raise ValueError("Acceptance criteria must be non-empty strings")
            clean = scrub(criterion.strip())
            if normalize(clean) not in {normalize(x) for x in criteria}:
                criteria.append(clean)
        with self._write_transaction():
            if parent_id and not self.db.execute("SELECT 1 FROM tasks WHERE id=?", (parent_id,)).fetchone():
                raise KeyError(f"Unknown parent task: {parent_id}")
            existing = self.db.execute("SELECT id,acceptance FROM tasks WHERE normalized=? AND status!='cancelled'", (key,)).fetchone()
            if existing:
                existing_criteria = json.loads(existing["acceptance"])
                known_criteria = {normalize(x) for x in existing_criteria}
                merged = existing_criteria + [criterion for criterion in criteria if normalize(criterion) not in known_criteria]
                if merged != existing_criteria:
                    self.db.execute("UPDATE tasks SET acceptance=?,updated_at=? WHERE id=?", (json.dumps(merged), now(), existing["id"]))
                    self._event(existing["id"], "acceptance_updated", json.dumps(merged))
                return str(existing["id"]), False
            task_id, timestamp = uuid.uuid4().hex[:12], now()
            self.db.execute("INSERT INTO tasks(id,description,normalized,status,priority,parent_id,acceptance,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?, ?,?)",
                            (task_id, description, key, max(0, min(priority, 4)), parent_id, json.dumps(criteria), timestamp, timestamp))
            self._event(task_id, "created", description)
            return task_id, True

    def sync_requirements(self, text: str) -> dict[str, list[str]]:
        """Append new requirements; preserve existing tasks and reconcile explicit changes/cancellations."""
        added: list[str] = []
        unchanged: list[str] = []
        cancelled: list[str] = []
        updated: list[str] = []
        ambiguous: list[str] = []
        if os.environ.get("CWM_DISABLE_CAPTURE", "").casefold() in {"1", "true", "yes"}:
            return {"added": [], "unchanged": [], "updated": [], "cancelled": [], "ambiguous": [], "disabled": ["capture disabled by CWM_DISABLE_CAPTURE"]}
        sanitized = scrub(text)
        sanitized = re.sub(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[:=]?\s*\[REDACTED\]", "", sanitized)
        sanitized = re.sub(r"\[REDACTED\]", "", sanitized)
        clauses = split_requirements(sanitized)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            def matching_tasks(target_norm: str) -> list[sqlite3.Row]:
                exact = self.db.execute("SELECT * FROM tasks WHERE normalized=? AND status!='cancelled'", (target_norm,)).fetchall()
                if exact:
                    return exact
                target_tokens = set(target_norm.split())
                if not target_tokens:
                    return []
                active = self.db.execute("SELECT * FROM tasks WHERE status!='cancelled'").fetchall()
                return [task for task in active if target_tokens.issubset(set(task["normalized"].split()))]
            for clause in clauses:
                norm = normalize(clause)
                cancel_prefix = r"^(?:(?:we|i)\s+)?(?:no longer need|do not need|don't need|cancel|drop|remove|forget)\b"
                if re.match(cancel_prefix, clause, re.I):
                    target = re.sub(cancel_prefix + r"\s+(?:the\s+)?", "", clause, flags=re.I)
                    target_norm = normalize(target)
                    candidates = matching_tasks(target_norm)
                    if len(candidates) == 1:
                        task = candidates[0]
                        self.db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now(), task["id"]))
                        self._event(task["id"], "cancelled", clause)
                        cancelled.append(task["id"])
                    elif len(candidates) > 1:
                        ambiguous.append(clause)
                    continue
                if re.match(r"^(?:change|update|replace)\b", clause, re.I):
                    changed = re.match(r"^(?:change|update|replace)\s+(.+?)\s+(?:to|with)\s+(.+)$", clause, re.I)
                    if changed:
                        old_text, new_text = changed.group(1).strip(" ."), changed.group(2).strip(" .")
                        old_norm = normalize(old_text)
                        candidates = matching_tasks(old_norm)
                    else:
                        candidates = []
                    if len(candidates) == 1:
                        task = candidates[0]
                        new_description = scrub(new_text)
                        destination = [candidate for candidate in matching_tasks(normalize(new_description)) if candidate["id"] != task["id"]]
                        if len(destination) == 1:
                            target_task = destination[0]
                            self.db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (now(), task["id"]))
                            detail = f"Requirement superseded by existing task {target_task['id']}: {new_description}"
                            self._event(task["id"], "requirement_merged", detail)
                            self._event(target_task["id"], "requirement_merged", f"Absorbed requirement from task {task['id']}")
                            cancelled.append(task["id"])
                            updated.append(target_task["id"])
                        elif len(destination) > 1:
                            ambiguous.append(clause)
                        else:
                            self.db.execute("UPDATE tasks SET description=?,normalized=?,status='pending',updated_at=? WHERE id=?",
                                            (new_description, normalize(new_description), now(), task["id"]))
                            self._event(task["id"], "requirement_changed", f"{task['description']} -> {new_description}")
                            updated.append(task["id"])
                        continue
                    elif len(candidates) > 1:
                        ambiguous.append(clause)
                        continue
                    else:
                        tid, created = self.add_task(clause, acceptance=["Updated requirement is implemented and verified"])
                    (added if created else unchanged).append(tid)
                    continue
                existing = self.db.execute("SELECT id FROM tasks WHERE normalized=? AND status!='cancelled'", (norm,)).fetchone()
                if existing:
                    unchanged.append(str(existing["id"]))
                    continue
                tid, created = self.add_task(clause, acceptance=["Requirement is implemented and evidence recorded"])
                (added if created else unchanged).append(tid)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return {"added": added, "unchanged": unchanged, "updated": updated, "cancelled": cancelled, "ambiguous": ambiguous}

    def list_tasks(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM tasks", []
        if statuses:
            statuses = list(statuses)
            sql += " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
        rows = self.db.execute(sql + " ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, priority, created_at", args)
        return [dict(r) for r in rows]

    def set_status(self, task_id: str, status: str, evidence: str = "") -> None:
        allowed = {"pending", "in_progress", "blocked", "completed", "cancelled"}
        if status not in allowed:
            raise ValueError(f"Invalid status: {status}")
        if status == "completed":
            evidence = scrub(evidence).strip()
            match = re.match(r"(?i)\s*(command|test|check|file|commit|result|verified|output)\s*[:=]\s*(.+)$", evidence)
            if not match or len(match.group(2).strip()) < 12 or not re.search(r"(?i)\b(passed|succeeded|verified|reviewed|matched|created|updated|committed|found|produced)\b", match.group(2)):
                raise ValueError("Completion evidence must name a source and result, such as 'test: pytest tests/test_store.py passed'. Evidence is recorded but cannot be independently validated by this tool.")
        with self._write_transaction():
            row = self.db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown task: {task_id}")
            if status == "completed":
                dependencies = json.loads(self.db.execute("SELECT dependencies FROM tasks WHERE id=?", (task_id,)).fetchone()[0])
                if dependencies:
                    placeholders = ",".join("?" for _ in dependencies)
                    incomplete = [r["id"] for r in self.db.execute(
                        f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status NOT IN ('completed','cancelled')",
                        dependencies,
                    )]
                    if incomplete:
                        raise ValueError("Cannot complete task while dependencies are unfinished or unresolved: " + ", ".join(incomplete))
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, now(), task_id))
            if evidence:
                evidence = scrub(evidence)
                old = self.db.execute("SELECT verification FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
                values = json.loads(old); values.append({"evidence": evidence, "at": now()})
                self.db.execute("UPDATE tasks SET verification=? WHERE id=?", (json.dumps(values), task_id))
            self._event(task_id, status, evidence or status)

    def set_dependencies(self, task_id: str, dependencies: Iterable[str]) -> None:
        deps = list(dict.fromkeys(dependencies))
        if task_id in deps:
            raise ValueError("A task cannot depend on itself")
        with self._write_transaction():
            if not self.db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError(f"Unknown task: {task_id}")
            known = {row[0] for row in self.db.execute("SELECT id FROM tasks")}
            if any(dep not in known for dep in deps):
                raise KeyError("Every dependency must be an existing task ID")
            graph = {row[0]: set(json.loads(row[1])) for row in self.db.execute("SELECT id,dependencies FROM tasks")}
            graph[task_id] = set(deps)
            def reaches(start: str, target: str) -> bool:
                pending = [start]
                seen: set[str] = set()
                while pending:
                    current = pending.pop()
                    if current == target:
                        return True
                    if current not in seen:
                        seen.add(current)
                        pending.extend(graph.get(current, ()))
                return False
            for dep in deps:
                if reaches(dep, task_id):
                    raise ValueError("Dependency would create a cycle")
            self.db.execute("UPDATE tasks SET dependencies=?,updated_at=? WHERE id=?", (json.dumps(deps), now(), task_id))
            self._event(task_id, "dependencies", json.dumps(deps))

    def add_decision(self, task_id: str, decision: str) -> None:
        decision = scrub(decision)
        with self._write_transaction():
            row = self.db.execute("SELECT decisions FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown task: {task_id}")
            values = json.loads(row[0]); values.append({"decision": decision, "at": now()})
            self.db.execute("UPDATE tasks SET decisions=?,updated_at=? WHERE id=?", (json.dumps(values), now(), task_id))
            self._event(task_id, "decision", decision)

    def add_relevant_files(self, task_id: str, paths: Iterable[str]) -> None:
        # Keep only project-relative paths to prevent unrelated local paths entering memory.
        safe_paths = []
        for path in paths:
            if not path:
                continue
            local_path = Path(path)
            windows_path = PureWindowsPath(path)
            if (local_path.is_absolute() or windows_path.is_absolute() or windows_path.drive or windows_path.root
                    or ".." in local_path.parts or ".." in windows_path.parts):
                continue
            safe_paths.append(scrub(path))
        with self._write_transaction():
            row = self.db.execute("SELECT relevant_files FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown task: {task_id}")
            values = list(dict.fromkeys(json.loads(row[0]) + safe_paths))
            self.db.execute("UPDATE tasks SET relevant_files=?,updated_at=? WHERE id=?", (json.dumps(values), now(), task_id))

    def context_bundle(self, query: str, char_budget: int = 6000) -> dict[str, Any]:
        """Return a small prioritized recovery bundle bounded by rendered characters."""
        from .vault import ProjectVault

        char_budget = max(256, min(char_budget, 100_000))
        matched = self.search(query, limit=100)
        vault = ProjectVault(self.path.parent / "vault")
        notes = vault.search_notes(query, limit=8) if query.strip() else vault.recent_notes(limit=3)
        active_by_id = {t["id"]: t for t in self.list_tasks(["in_progress", "blocked", "pending"])}
        ordered = [active_by_id.pop(t["id"]) for t in matched if t["id"] in active_by_id]
        ordered.extend(sorted(active_by_id.values(), key=lambda t: (t["status"] != "in_progress", t["status"] != "blocked", t["priority"])))
        latest_checkpoint = self.latest_checkpoint()
        cp = ({key: latest_checkpoint[key] for key in ("summary", "last_action", "next_action", "created_at")}
              if latest_checkpoint else None)
        if cp:
            cp["summary"] = cp["summary"][:160]
            cp["last_action"] = cp["last_action"][:120]
            cp["next_action"] = cp["next_action"][:120]
        selected = []
        selected_notes: list[dict[str, Any]] = []

        def rendered(items: list[dict[str, Any]], omitted: int,
                     note_items: list[dict[str, Any]], omitted_notes: int) -> str:
            return json.dumps({"checkpoint": cp, "tasks": items, "notes": note_items,
                               "omitted_tasks": omitted, "omitted_notes": omitted_notes,
                               "character_budget": char_budget}, ensure_ascii=False)

        task_budget = char_budget - min(1800, char_budget // 3) if notes else char_budget
        for task in ordered:
            if len(rendered(selected + [task], len(ordered) - len(selected) - 1,
                            [], len(notes))) > task_budget:
                continue
            selected.append(task)

        for note in notes:
            candidate = selected_notes + [note]
            if len(rendered(selected, len(ordered) - len(selected), candidate,
                            len(notes) - len(candidate))) <= char_budget:
                selected_notes.append(note)

        selected_ids = {task["id"] for task in selected}
        for task in ordered:
            if task["id"] in selected_ids:
                continue
            candidate = selected + [task]
            if len(rendered(candidate, len(ordered) - len(candidate), selected_notes,
                            len(notes) - len(selected_notes))) <= char_budget:
                selected.append(task)
                selected_ids.add(task["id"])

        result = {"checkpoint": cp, "tasks": selected, "notes": selected_notes,
                  "omitted_tasks": len(ordered) - len(selected),
                  "omitted_notes": len(notes) - len(selected_notes), "character_budget": char_budget}
        for field in ("summary", "last_action", "next_action"):
            while cp and len(json.dumps(result, ensure_ascii=False)) > char_budget and cp[field]:
                cp[field] = cp[field][:max(0, len(cp[field]) - 64)]
                result["checkpoint"] = cp
        if len(json.dumps(result, ensure_ascii=False)) > char_budget and cp:
            cp = None
            result["checkpoint"] = None
        while selected and len(json.dumps(result, ensure_ascii=False)) > char_budget:
            selected.pop()
            result["tasks"] = selected
            result["omitted_tasks"] = len(ordered) - len(selected)
        while selected_notes and len(json.dumps(result, ensure_ascii=False)) > char_budget:
            selected_notes.pop()
            result["notes"] = selected_notes
            result["omitted_notes"] = len(notes) - len(selected_notes)
        return result

    def checkpoint(self, summary: str, last_action: str = "", next_action: str = "") -> str:
        checkpoint_id = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", (checkpoint_id, scrub(summary), scrub(last_action), scrub(next_action), now()))
        return checkpoint_id

    def latest_checkpoint(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM checkpoints ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def recover(self, budget: int = 12) -> dict[str, Any]:
        from .vault import ProjectVault

        tasks = self.list_tasks(["pending", "in_progress", "blocked"])
        recent = self.db.execute("SELECT kind,detail,created_at FROM events ORDER BY id DESC LIMIT 8").fetchall()
        return {"project_root": str(self.path.parent.parent), "active_tasks": tasks[:max(0, budget)],
                "remaining_count": len(tasks), "checkpoint": self.latest_checkpoint(),
                "recent_events": [dict(r) for r in recent],
                "recent_notes": ProjectVault(self.path.parent / "vault").recent_notes(limit=3)}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(0, limit)
        terms = [x for x in normalize(query).split() if len(x) > 1]
        rows = self.db.execute("SELECT id,description,status,priority,updated_at FROM tasks WHERE status!='cancelled'").fetchall()
        scored = []
        for row in rows:
            tokens = set(normalize(row["description"]).split())
            score = sum(1 for term in terms if term in tokens)
            if score:
                scored.append((score, dict(row)))
        return [item for _, item in sorted(scored, key=lambda x: (-x[0], x[1]["priority"], x[1]["id"]))[:limit]]


def project_store(root: str | Path | None = None) -> Store:
    configured_root = root if root is not None else os.environ.get("CWM_PROJECT_ROOT")
    root_path = Path(configured_root or os.getcwd()).expanduser().resolve()
    if root_path.is_file():
        root_path = root_path.parent
    return Store(root_path / ".context-manager" / "state.sqlite3")
