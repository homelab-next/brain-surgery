"""Split a markdown file into blocks -- the unit you select, screen and quarantine.

Block kinds: ``frontmatter`` (leading ``---`` YAML), ``heading``, ``item`` (one list
item with its continuation lines), ``row`` (one table row), ``code`` (a whole fenced
block), ``paragraph``. Blank lines are separators and belong to no block.

A block's identity is its **content hash** plus its occurrence number among identical
blocks in the same file -- not its line number. Agents keep writing to these files, so
line numbers drift between a scan and a quarantine; the hash is what gets matched.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Block:
    ord: int              # position among the file's blocks
    kind: str
    start: int            # 1-based first line
    end: int              # 1-based last line (inclusive)
    text: str
    hash: str
    occ: int              # 0 for the first block with this hash in the file, 1 for the next...
    section: str          # heading trail, e.g. "Backups > Schedule"

    @property
    def chars(self) -> int:
        return len(self.text)


def block_hash(text: str) -> str:
    norm = "\n".join(line.rstrip() for line in text.strip("\n").split("\n"))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def split_blocks(text: str) -> list[Block]:
    lines = text.split("\n")
    spans: list[tuple[str, int, int]] = []   # kind, start idx, end idx (inclusive), 0-based
    headings: dict[int, str] = {}            # start idx -> heading text, for section trails
    i, n = 0, len(lines)

    if n and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() in ("---", "..."):
                spans.append(("frontmatter", 0, j))
                i = j + 1
                break

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _FENCE.match(line):
            fence = _FENCE.match(line).group(1)
            j = i + 1
            while j < n and not lines[j].lstrip().startswith(fence):
                j += 1
            spans.append(("code", i, min(j, n - 1)))
            i = j + 1
            continue
        m = _HEADING.match(line)
        if m:
            spans.append(("heading", i, i))
            headings[i] = m.group(2).strip()
            i += 1
            continue
        if line.lstrip().startswith("|"):
            spans.append(("row", i, i))
            i += 1
            continue
        if _ITEM.match(line):
            # continuation lines join the item; a nested list marker starts its own item
            j = i + 1
            while j < n and lines[j].strip() and not _starts_block(lines[j]):
                j += 1
            spans.append(("item", i, j - 1))
            i = j
            continue
        j = i + 1
        while j < n and lines[j].strip() and not _starts_block(lines[j]):
            j += 1
        spans.append(("paragraph", i, j - 1))
        i = j

    blocks: list[Block] = []
    seen: dict[str, int] = {}
    trail: list[tuple[int, str]] = []        # (level, title)
    for ord_, (kind, s, e) in enumerate(spans):
        if kind == "heading":
            level = len(_HEADING.match(lines[s]).group(1))
            trail = [t for t in trail if t[0] < level] + [(level, headings[s])]
        body = "\n".join(lines[s:e + 1])
        h = block_hash(body)
        occ = seen.get(h, 0)
        seen[h] = occ + 1
        section = " > ".join(t for _, t in trail)
        blocks.append(Block(ord=ord_, kind=kind, start=s + 1, end=e + 1, text=body, hash=h,
                            occ=occ, section=section))
    return blocks


def _starts_block(line: str) -> bool:
    return bool(_HEADING.match(line) or _ITEM.match(line) or _FENCE.match(line)
                or line.lstrip().startswith("|"))


def remove_lines(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove 1-based inclusive line spans, then collapse any run of blank lines the
    removal created down to one."""
    lines = text.split("\n")
    drop: set[int] = set()
    for s, e in spans:
        drop.update(range(s - 1, e))
    kept = [ln for idx, ln in enumerate(lines) if idx not in drop]
    out: list[str] = []
    for ln in kept:
        if not ln.strip() and out and not out[-1].strip():
            continue
        out.append(ln)
    while len(out) > 1 and not out[0].strip():
        out.pop(0)
    return "\n".join(out)


TIGHT = ("item", "row")


def insert_block(text: str, block_text: str, after_line: int, kind: str) -> str:
    """Insert ``block_text`` after 1-based line ``after_line`` (0 = top).

    A list item or table row sits tight against a neighbour of the same kind; against
    anything else, and for every other kind, it is separated by one blank line."""
    lines = text.split("\n") if text else []
    new = block_text.split("\n")
    after_line = max(0, min(after_line, len(lines)))
    before, rest = lines[:after_line], lines[after_line:]
    blocks = split_blocks(text) if text else []
    prev_kind = next((b.kind for b in blocks if b.end == after_line), None)
    next_kind = next((b.kind for b in blocks if b.start == after_line + 1), None)
    tight = kind in TIGHT
    if before and before[-1].strip() and not (tight and prev_kind == kind):
        new = [""] + new
    if rest and rest[0].strip() and not (tight and next_kind == kind):
        new = new + [""]
    return "\n".join(before + new + rest)
