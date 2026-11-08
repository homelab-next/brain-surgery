"""Text matching used to link a block to what it was copied from.

Deliberately simple and local: whitespace-normalized substring first (an exact copy),
then token-shingle Jaccard (a paraphrase or a reflow). No model, no network. The score
is what confidence a provenance link carries.
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def _shingles(tokens: list[str], n: int = 3) -> set[str]:
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def similarity(block: str, candidate: str) -> tuple[float, str]:
    """Return (score in [0,1], method). ``candidate`` is usually much larger than
    ``block`` (a whole read file), so this measures how much of the block it contains."""
    b, c = normalize(block), normalize(candidate)
    if not b or not c:
        return 0.0, "none"
    if b in c:
        return 1.0, "exact"
    bt, ct = b.split(), c.split()
    bs, cs = _shingles(bt), _shingles(ct)
    if not bs:
        return 0.0, "none"
    overlap = len(bs & cs) / len(bs)        # share of the block's shingles present
    return overlap, "overlap"
