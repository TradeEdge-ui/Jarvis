"""Persistent memory with retrieval (SQLite FTS5 / BM25), not prompt stuffing.

Categories map to the spec:
  short-term   -> `messages` table (current conversation window)
  long_term    -> stable preferences, goals, workflows
  business     -> companies, products, customers, projects, KPIs
  device       -> connected devices and what they may do
  knowledge    -> ingested documents / SOPs / notes (chunked)
  operational  -> the audit log (what FRIDAY did) — searched via `search_operational`
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from friday.clock import Clock, iso, parse_iso
from friday.db import Database

CATEGORIES = ("long_term", "business", "device", "knowledge")

_STOP = set("""a an the and or but if then of to in on at for with about from by is are was were be been it its this that
these those i me my we our you your do does did have has had what which who whom how when where why can could would
should will shall may might please tell show give find any some there their them they he she his her as not no yes
friday remember recall know""".split())
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def _terms(query: str) -> list[str]:
    toks = [t.lower() for t in _TOKEN.findall(query)]
    return [t for t in toks if t not in _STOP and len(t) > 1]


class MemoryStore:
    def __init__(self, db: Database, clock: Clock):
        self.db, self.clock = db, clock

    # ---- category switches -------------------------------------------------
    def disabled_categories(self) -> set[str]:
        return set(self.db.get_setting("memory_disabled", []))

    def set_category_enabled(self, category: str, enabled: bool) -> None:
        self._check_cat(category)
        dis = self.disabled_categories()
        (dis.discard if enabled else dis.add)(category)
        self.db.set_setting("memory_disabled", sorted(dis))

    def categories_status(self) -> list[dict]:
        dis = self.disabled_categories()
        out = []
        for c in CATEGORIES:
            n = self.db.one("SELECT COUNT(*) c FROM memories WHERE category=? AND deleted=0", (c,))["c"]
            out.append({"category": c, "enabled": c not in dis, "count": n})
        return out

    @staticmethod
    def _check_cat(category: str) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"unknown memory category '{category}' (valid: {', '.join(CATEGORIES)})")

    # ---- CRUD ----------------------------------------------------------------
    def add(self, content: str, category: str = "long_term", title: str | None = None, tags: list[str] | None = None,
            project: str | None = None, source: str = "user", importance: float = 0.5) -> int:
        self._check_cat(category)
        content = content.strip()
        if not content:
            raise ValueError("empty memory")
        if category in self.disabled_categories():
            raise PermissionError(f"memory category '{category}' is disabled")
        # avoid exact duplicates
        dup = self.db.one("SELECT id FROM memories WHERE category=? AND content=? AND deleted=0", (category, content))
        if dup:
            return int(dup["id"])
        now = iso(self.clock.now())
        cur = self.db.execute(
            "INSERT INTO memories(category,title,content,tags,project,source,importance,created,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (category, title or content[:60], content, " ".join(tags or []), project, source, importance, now, now))
        return int(cur.lastrowid)

    def get(self, memory_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM memories WHERE id=? AND deleted=0", (memory_id,))
        return dict(r) if r else None

    def update(self, memory_id: int, **fields) -> bool:
        allowed = {"content", "title", "tags", "project", "importance", "category"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "category" in sets:
            self._check_cat(sets["category"])
        if "tags" in sets and isinstance(sets["tags"], list):
            sets["tags"] = " ".join(sets["tags"])
        if not sets:
            return False
        sets["updated"] = iso(self.clock.now())
        cols = ", ".join(f"{k}=?" for k in sets)
        cur = self.db.execute(f"UPDATE memories SET {cols} WHERE id=? AND deleted=0", (*sets.values(), memory_id))
        return cur.rowcount == 1

    def delete(self, memory_id: int, hard: bool = False) -> bool:
        if hard:
            return self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount == 1
        return self.db.execute("UPDATE memories SET deleted=1, updated=? WHERE id=? AND deleted=0",
                               (iso(self.clock.now()), memory_id)).rowcount == 1

    def list(self, category: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        sql, p = "SELECT * FROM memories WHERE deleted=0", []
        if category:
            sql += " AND category=?"; p.append(category)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"; p += [limit, offset]
        return [dict(r) for r in self.db.query(sql, tuple(p))]

    def export(self) -> dict:
        return {
            "exported_at": iso(self.clock.now()),
            "memories": [dict(r) for r in self.db.query("SELECT * FROM memories WHERE deleted=0 ORDER BY id")],
            "tasks": [dict(r) for r in self.db.query("SELECT * FROM tasks ORDER BY id")],
        }

    # ---- retrieval -----------------------------------------------------------
    def search(self, query: str, categories: list[str] | None = None, limit: int = 8,
               project: str | None = None) -> list[dict]:
        """BM25 over title/content/tags, lightly boosted by importance and recency."""
        terms = _terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in terms)
        dis = self.disabled_categories()
        cats = [c for c in (categories or CATEGORIES) if c not in dis]
        if not cats:
            return []
        ph = ",".join("?" * len(cats))
        sql = (f"SELECT m.*, bm25(memories_fts, 3.0, 1.0, 2.0) AS score FROM memories_fts "
               f"JOIN memories m ON m.id = memories_fts.rowid "
               f"WHERE memories_fts MATCH ? AND m.deleted=0 AND m.category IN ({ph})")
        params: list = [match, *cats]
        if project:
            sql += " AND m.project=?"; params.append(project)
        sql += " ORDER BY score LIMIT ?"; params.append(limit * 3)
        rows = [dict(r) for r in self.db.query(sql, tuple(params))]
        now = self.clock.now()
        for r in rows:
            age_days = max(0.0, (now - parse_iso(r["updated"])).total_seconds() / 86400)
            # bm25 is negative (more negative = better); convert to a positive relevance
            rel = -r["score"]
            r["relevance"] = rel * (0.7 + 0.6 * float(r["importance"])) * (1.0 + 0.3 * math.exp(-age_days / 30))
        rows.sort(key=lambda r: r["relevance"], reverse=True)
        return rows[:limit]

    def search_operational(self, query: str, limit: int = 10) -> list[dict]:
        terms = _terms(query)
        if not terms:
            return []
        clause = " OR ".join(["(user_command LIKE ? OR summary LIKE ? OR action LIKE ?)"] * len(terms))
        params: list = []
        for t in terms:
            params += [f"%{t}%"] * 3
        rows = self.db.query(f"SELECT id,ts,tool,user_command,summary,status,verified FROM audit_log "
                             f"WHERE {clause} ORDER BY id DESC LIMIT ?", (*params, limit))
        return [dict(r) for r in rows]

    def pinned(self, limit: int = 6) -> list[dict]:
        """High-importance stable facts that are always worth having in context."""
        dis = self.disabled_categories()
        rows = self.db.query("SELECT * FROM memories WHERE deleted=0 AND importance>=0.8 AND category='long_term' "
                             "ORDER BY importance DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows if r["category"] not in dis]

    # ---- knowledge base ---------------------------------------------------
    TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml",
                     ".ini", ".log", ".sql", ".ps1", ".sh", ".toml", ".xml"}

    def ingest_text(self, text: str, source: str, project: str | None = None, chunk_chars: int = 900) -> list[int]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks, cur = [], ""
        for p in paras:
            while len(p) > chunk_chars:
                chunks.append(p[:chunk_chars]); p = p[chunk_chars:]
            if len(cur) + len(p) + 2 > chunk_chars and cur:
                chunks.append(cur); cur = p
            else:
                cur = f"{cur}\n\n{p}" if cur else p
        if cur:
            chunks.append(cur)
        name = Path(source).name
        return [self.add(c, "knowledge", title=f"{name} #{i + 1}", project=project, source=source, importance=0.4)
                for i, c in enumerate(chunks)]
