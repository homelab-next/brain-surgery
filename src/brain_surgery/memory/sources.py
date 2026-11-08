"""Memory sources: where memory files live, who loads them, and how often.

Every file gets a ``loaded`` level -- how much exposure its content has:

* ``always``    -- in context at every session start (CLAUDE.md, a MEMORY.md index,
                   skill names and descriptions);
* ``on_demand`` -- read when relevant (topic memory files, skill bodies, vault notes);
* ``never``     -- kept but not loaded (archives).

``managed_by`` marks a deployed or generated copy whose real source lives elsewhere
(an Ansible role file, a generator input). Editing the copy is pointless -- the next
deploy restores it -- so quarantine refuses managed copies unless forced, and says
where to make the change instead.
"""
from __future__ import annotations

import fnmatch
import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

LOADED = ("always", "on_demand", "never")
KINDS = ("claude_memory_dir", "agent_memory_root", "files", "skills", "markdown_dir")
DEFAULT_EXCLUDE = ["*.archive-*.md", "*.scoped.*.md"]


@dataclass(frozen=True)
class MemoryFile:
    source_id: str
    kind: str
    role: str                 # index | topic | instructions | skill | note
    agent: str
    path: Path
    loaded: str
    managed_by: str | None = None
    frontmatter_loaded: str | None = None   # e.g. skills: front-matter is always loaded


@dataclass(frozen=True)
class SourceConfig:
    id: str
    kind: str
    path: str | None = None
    paths: tuple[str, ...] = ()
    glob: str = "**/*.md"
    agent: str | None = None
    loaded: str | None = None
    role: str | None = None
    managed_by: str | None = None
    exclude: tuple[str, ...] = tuple(DEFAULT_EXCLUDE)

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "SourceConfig":
        if d.get("kind") not in KINDS:
            raise ValueError(f"source {d.get('id')!r}: kind must be one of {KINDS}")
        if not d.get("id"):
            raise ValueError("every source needs an id")
        if d.get("loaded") is not None and d["loaded"] not in LOADED:
            raise ValueError(f"source {d['id']!r}: loaded must be one of {LOADED}")
        paths = d.get("paths") or []
        return SourceConfig(
            id=str(d["id"]), kind=d["kind"], path=d.get("path"),
            paths=tuple(str(p) for p in paths), glob=str(d.get("glob") or "**/*.md"),
            agent=d.get("agent"), loaded=d.get("loaded"), role=d.get("role"),
            managed_by=d.get("managed_by"),
            exclude=tuple(d.get("exclude") or DEFAULT_EXCLUDE),
        )

    def roots(self) -> list[Path]:
        if self.kind == "files":
            return sorted({Path(os.path.expanduser(p)).parent for p in self.paths})
        return [Path(os.path.expanduser(self.path))] if self.path else []


def _excluded(p: Path, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(p.name, pat) or fnmatch.fnmatch(str(p), pat) for pat in patterns)


def _memory_dir(src: SourceConfig, root: Path, agent: str) -> Iterator[MemoryFile]:
    for p in sorted(root.rglob("*.md")):
        if not p.is_file() or _excluded(p, src.exclude):
            continue
        is_index = p.name == "MEMORY.md"
        yield MemoryFile(src.id, src.kind, "index" if is_index else "topic", agent, p,
                         src.loaded or ("always" if is_index else "on_demand"), src.managed_by)


def iter_files(src: SourceConfig) -> Iterator[MemoryFile]:
    if src.kind == "claude_memory_dir":
        root = Path(os.path.expanduser(src.path or ""))
        if root.is_dir():
            yield from _memory_dir(src, root, src.agent or "interactive")
    elif src.kind == "agent_memory_root":
        root = Path(os.path.expanduser(src.path or ""))
        if root.is_dir():
            for agent_dir in sorted(d for d in root.iterdir() if d.is_dir()):
                yield from _memory_dir(src, agent_dir, agent_dir.name)
    elif src.kind == "files":
        for pattern in src.paths:
            for match in sorted(glob.glob(os.path.expanduser(pattern), recursive=True)):
                p = Path(match)
                if p.is_file() and not _excluded(p, src.exclude):
                    yield MemoryFile(src.id, src.kind, src.role or "instructions",
                                     src.agent or "*", p, src.loaded or "always", src.managed_by)
    elif src.kind == "skills":
        root = Path(os.path.expanduser(src.path or ""))
        if root.is_dir():
            for p in sorted(root.glob("*/SKILL.md")):
                yield MemoryFile(src.id, src.kind, "skill", src.agent or "*", p,
                                 src.loaded or "on_demand", src.managed_by,
                                 frontmatter_loaded="always")
    elif src.kind == "markdown_dir":
        root = Path(os.path.expanduser(src.path or ""))
        if root.is_dir():
            for p in sorted(root.glob(src.glob)):
                if p.is_file() and not _excluded(p, src.exclude):
                    yield MemoryFile(src.id, src.kind, src.role or "note", src.agent or "*", p,
                                     src.loaded or "on_demand", src.managed_by)


def check_outside_roots(sources: list[SourceConfig], paths: Mapping[str, Path]) -> list[str]:
    """State files (index, holds) must not sit inside a scanned root, or quarantined
    text could be scanned -- and loaded -- right back."""
    problems = []
    for label, p in paths.items():
        rp = p.resolve()
        for s in sources:
            for root in s.roots():
                try:
                    rp.relative_to(root.resolve())
                except ValueError:
                    continue
                problems.append(f"{label} ({p}) is inside scanned root {root} of source {s.id!r}")
    return problems
