"""Filesystem tools. Every write is verified by re-reading; overwrites and deletes are always recoverable."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from friday.security.policy import Classification, Effect, Risk
from friday.tools.base import Tool, ToolContext, ToolResult, Verification
from friday.tools.pathpolicy import PathDenied

MAX_READ_BYTES = 5_000_000


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _stamp(ctx: ToolContext) -> str:
    return ctx.now.strftime("%Y%m%d-%H%M%S")


def _backup(ctx: ToolContext, p: Path, kind: str = "overwritten") -> Path:
    dest_dir = ctx.svc.config.path("Backups") / kind / _stamp(ctx)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{p.stem}.{i}{p.suffix}"; i += 1
    shutil.copy2(p, dest)
    return dest


class _Base(Tool):
    group = "filesystem"

    def _read_path(self, ctx: ToolContext, raw: str) -> Path:
        return ctx.svc.paths.check_read(raw)

    def _write_path(self, ctx: ToolContext, raw: str) -> Path:
        return ctx.svc.paths.check_write(raw)


# ---------------------------------------------------------------- list
class FsList(_Base):
    name = "fs_list"
    scope = "filesystem.read"
    description = "List files and folders in a directory (name, size, modified time)."

    class Args(BaseModel):
        path: str = Field(description="Folder path. Relative paths are inside the FRIDAY workspace. "
                                      "'desktop', 'downloads', 'documents' are accepted.")
        pattern: str = Field(default="*", description="Glob filter on names, e.g. *.xlsx")
        recursive: bool = False
        limit: int = Field(default=100, ge=1, le=500)

    def classify(self, args, ctx):
        try:
            self._read_path(ctx, args.path)
        except PathDenied as e:
            return Classification(blocked=str(e))
        return Classification()

    def run(self, args, ctx):
        p = self._read_path(ctx, args.path)
        if not p.is_dir():
            return ToolResult.fail(f"{p} is not a directory")
        it = p.rglob("*") if args.recursive else p.iterdir()
        rows = []
        for e in it:
            if not fnmatch.fnmatch(e.name.lower(), args.pattern.lower()):
                continue
            try:
                if ctx.svc.paths._protected(e.resolve()):
                    continue
                st = e.stat()
            except OSError:
                continue
            rows.append({"name": str(e.relative_to(p)), "type": "dir" if e.is_dir() else "file",
                         "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
            if len(rows) >= args.limit:
                break
        rows.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))
        return ToolResult.success(f"{len(rows)} item(s) in {p}", path=str(p), items=rows,
                                  truncated=len(rows) >= args.limit)


# ---------------------------------------------------------------- read
class FsRead(_Base):
    name = "fs_read"
    scope = "filesystem.read"
    description = "Read a text file (txt, md, csv, json, code, logs). Use spreadsheet_* tools for xlsx."

    class Args(BaseModel):
        path: str
        max_chars: int = Field(default=20000, ge=100, le=100000)

    def classify(self, args, ctx):
        try:
            self._read_path(ctx, args.path)
        except PathDenied as e:
            return Classification(blocked=str(e))
        return Classification()

    def run(self, args, ctx):
        p = self._read_path(ctx, args.path)
        if not p.is_file():
            return ToolResult.fail(f"{p} is not a file")
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            return ToolResult.fail(f"{p.name} is {size} bytes; too large to read directly")
        raw = p.read_bytes()
        if b"\x00" in raw[:4096]:
            return ToolResult.fail(f"{p.name} looks like a binary file; cannot display as text")
        text = raw.decode("utf-8", errors="replace")
        clipped = len(text) > args.max_chars
        return ToolResult.success(f"Read {p.name} ({size} bytes{', truncated' if clipped else ''})",
                                  path=str(p), content=text[:args.max_chars], size=size, truncated=clipped)


# ---------------------------------------------------------------- write
class FsWrite(_Base):
    name = "fs_write"
    scope = "filesystem.write"
    description = ("Create a text file, or overwrite one when mode='overwrite' (the old version is backed up first). "
                   "Parent folders are created.")

    class Args(BaseModel):
        path: str
        content: str
        mode: Literal["create", "overwrite"] = Field(
            default="create", description="'create' fails if the file exists; 'overwrite' backs up then replaces.")

    def classify(self, args, ctx):
        try:
            p = self._write_path(ctx, args.path)
        except PathDenied as e:
            return Classification(blocked=str(e))
        inside = ctx.svc.paths.in_workspace(p)
        risk = Risk.LOW
        note = ""
        if p.exists() and args.mode == "overwrite" and not inside:
            risk, note = Risk.REVIEW, f"This overwrites an existing file outside the workspace ({p}). A backup is kept."
        return Classification(risk, Effect.INTERNAL if inside else Effect.EXTERNAL, note=note)

    def run(self, args, ctx):
        p = self._write_path(ctx, args.path)
        data = args.content.encode("utf-8")
        backup = None
        if p.exists():
            if p.is_dir():
                return ToolResult.fail(f"{p} is a directory")
            if args.mode == "create":
                return ToolResult.fail(f"{p} already exists. Use mode='overwrite' to replace it (a backup is kept).")
            backup = _backup(ctx, p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".friday-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return ToolResult.success(f"Wrote {len(data)} bytes to {p}", path=str(p), bytes=len(data),
                                  sha256=_sha(data), backup=str(backup) if backup else None)

    def verify(self, args, result, ctx):
        p = Path(result.data["path"])
        if not p.is_file():
            return Verification(False, "file exists check", f"{p} does not exist after write")
        actual = _sha(p.read_bytes())
        if actual != result.data["sha256"]:
            return Verification(False, "re-read + sha256", "content on disk differs from what was written")
        return Verification(True, "re-read + sha256", f"{p.stat().st_size} bytes on disk, content matches")


# ---------------------------------------------------------------- append
class FsAppend(_Base):
    name = "fs_append"
    scope = "filesystem.write"
    description = "Append text to the end of a file (created if missing)."

    class Args(BaseModel):
        path: str
        content: str

    def classify(self, args, ctx):
        try:
            p = self._write_path(ctx, args.path)
        except PathDenied as e:
            return Classification(blocked=str(e))
        return Classification(Risk.LOW, Effect.INTERNAL if ctx.svc.paths.in_workspace(p) else Effect.EXTERNAL)

    def run(self, args, ctx):
        p = self._write_path(ctx, args.path)
        if p.is_dir():
            return ToolResult.fail(f"{p} is a directory")
        before = p.stat().st_size if p.exists() else 0
        backup = _backup(ctx, p) if before else None
        p.parent.mkdir(parents=True, exist_ok=True)
        data = args.content.encode("utf-8")
        with open(p, "ab") as f:
            f.write(data)
        return ToolResult.success(f"Appended {len(data)} bytes to {p}", path=str(p), before=before,
                                  appended=len(data), tail_sha=_sha(data), backup=str(backup) if backup else None)

    def verify(self, args, result, ctx):
        p = Path(result.data["path"])
        if not p.is_file():
            return Verification(False, "file exists check", "file missing after append")
        raw = p.read_bytes()
        d = result.data
        ok = len(raw) == d["before"] + d["appended"] and _sha(raw[d["before"]:]) == d["tail_sha"]
        return Verification(ok, "size + tail hash", f"{len(raw)} bytes on disk" if ok else "size/tail mismatch")


# ---------------------------------------------------------------- mkdir
class FsMkdir(_Base):
    name = "fs_mkdir"
    scope = "filesystem.write"
    description = "Create a folder (and parents)."

    class Args(BaseModel):
        path: str

    def classify(self, args, ctx):
        try:
            p = self._write_path(ctx, args.path)
        except PathDenied as e:
            return Classification(blocked=str(e))
        return Classification(Risk.SAFE, Effect.INTERNAL if ctx.svc.paths.in_workspace(p) else Effect.EXTERNAL)

    def run(self, args, ctx):
        p = self._write_path(ctx, args.path)
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult.success(f"Folder ready: {p}", path=str(p))

    def verify(self, args, result, ctx):
        ok = Path(result.data["path"]).is_dir()
        return Verification(ok, "directory exists check", "" if ok else "directory missing")


# ---------------------------------------------------------------- move
class FsMove(_Base):
    name = "fs_move"
    scope = "filesystem.write"
    description = "Move or rename a file/folder. Refuses to overwrite an existing destination."

    class Args(BaseModel):
        source: str
        destination: str

    def classify(self, args, ctx):
        try:
            s = self._write_path(ctx, args.source)
            d = self._write_path(ctx, args.destination)
        except PathDenied as e:
            return Classification(blocked=str(e))
        inside = ctx.svc.paths.in_workspace(s) and ctx.svc.paths.in_workspace(d)
        return Classification(Risk.LOW, Effect.INTERNAL if inside else Effect.EXTERNAL)

    def run(self, args, ctx):
        s = self._write_path(ctx, args.source)
        d = self._write_path(ctx, args.destination)
        if not s.exists():
            return ToolResult.fail(f"{s} does not exist")
        if d.is_dir():
            d = d / s.name
        if d.exists():
            return ToolResult.fail(f"{d} already exists; refusing to overwrite")
        d.parent.mkdir(parents=True, exist_ok=True)
        size = s.stat().st_size if s.is_file() else None
        shutil.move(str(s), str(d))
        return ToolResult.success(f"Moved {s.name} → {d}", source=str(s), destination=str(d), size=size)

    def verify(self, args, result, ctx):
        s, d = Path(result.data["source"]), Path(result.data["destination"])
        ok = (not s.exists()) and d.exists() and (result.data["size"] is None or d.stat().st_size == result.data["size"])
        return Verification(ok, "source gone + destination present", f"{d}" if ok else "move not confirmed")


# ---------------------------------------------------------------- search
class FsSearch(_Base):
    name = "fs_search"
    scope = "filesystem.read"
    description = "Find files by name pattern and/or text content under a folder. Newest first."

    class Args(BaseModel):
        root: str = Field(description="Folder to search under")
        name_pattern: str = Field(default="*", description="Glob on file names, e.g. *revenue*.xlsx")
        contains: str | None = Field(default=None, description="Only text files containing this string")
        limit: int = Field(default=25, ge=1, le=200)

    def classify(self, args, ctx):
        try:
            self._read_path(ctx, args.root)
        except PathDenied as e:
            return Classification(blocked=str(e))
        return Classification()

    def run(self, args, ctx):
        root = self._read_path(ctx, args.root)
        if not root.is_dir():
            return ToolResult.fail(f"{root} is not a directory")
        hits = []
        needle = args.contains.lower() if args.contains else None
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}
                       and not ctx.svc.paths._protected(Path(dirpath) / d)]
            for fn in files:
                if not fnmatch.fnmatch(fn.lower(), args.name_pattern.lower()):
                    continue
                fp = Path(dirpath) / fn
                if ctx.svc.paths._protected(fp):
                    continue
                try:
                    st = fp.stat()
                    if needle:
                        if st.st_size > 2_000_000:
                            continue
                        if needle not in fp.read_bytes().decode("utf-8", errors="ignore").lower():
                            continue
                except OSError:
                    continue
                hits.append((st.st_mtime, fp, st.st_size))
        hits.sort(reverse=True)
        items = [{"path": str(fp), "size": sz, "modified": datetime.fromtimestamp(m).isoformat(timespec="seconds")}
                 for m, fp, sz in hits[:args.limit]]
        return ToolResult.success(f"Found {len(hits)} file(s) under {root}"
                                  + (f" (showing {len(items)})" if len(hits) > len(items) else ""),
                                  root=str(root), items=items, total=len(hits))


# ---------------------------------------------------------------- delete
class FsDelete(_Base):
    name = "fs_delete"
    scope = "filesystem.delete"
    description = "Delete a file or folder. It is moved to Backups/trash (recoverable), never permanently removed."

    class Args(BaseModel):
        path: str

    def classify(self, args, ctx):
        try:
            p = self._write_path(ctx, args.path)
        except PathDenied as e:
            return Classification(blocked=str(e))
        if p == ctx.svc.config.home or p == Path.home() or p.parent == p:
            return Classification(blocked="refusing to delete a root/home/workspace folder")
        if p.is_dir():
            return Classification(Risk.MANDATORY, Effect.EXTERNAL,
                                  note=f"Deleting the folder {p} and everything in it. Approval is always required.")
        return Classification(Risk.REVIEW, Effect.EXTERNAL, note=f"Deleting {p} (it will be kept in Backups/trash).")

    def run(self, args, ctx):
        p = self._write_path(ctx, args.path)
        if not p.exists():
            return ToolResult.fail(f"{p} does not exist")
        dest_dir = ctx.svc.config.path("Backups") / "trash" / _stamp(ctx)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        i = 1
        while dest.exists():
            dest = dest_dir / f"{p.name}.{i}"; i += 1
        shutil.move(str(p), str(dest))
        return ToolResult.success(f"Moved {p} to trash ({dest})", path=str(p), trash=str(dest))

    def verify(self, args, result, ctx):
        ok = (not Path(result.data["path"]).exists()) and Path(result.data["trash"]).exists()
        return Verification(ok, "original gone + trash copy present", result.data["trash"] if ok else "not confirmed")


TOOLS = [FsList, FsRead, FsWrite, FsAppend, FsMkdir, FsMove, FsSearch, FsDelete]
