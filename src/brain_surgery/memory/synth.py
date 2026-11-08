"""Synthetic memory lab: a realistic, harmless memory tree with known labels.

The "restricted topic" is a **canary**: a fictional vendor contract, "Kestrel". It
stands in for whatever your policy keeps out of always-loaded memory, so the whole
pipeline (scan, duplicates, screen, eval, quarantine, regrowth) can be exercised and
measured without generating any genuinely sensitive text. The canary appears in every
shape the screen spec distinguishes, next to routine notes and deliberate hard negatives:

* summary          -- restricted                          -> review
* steps            -- restricted + step-by-step detail    -> quarantine
* verbatim         -- restricted + word-for-word quote    -> quarantine
* steps_verbatim   -- restricted + both                   -> quarantine
* pointer          -- restricted, but only points away    -> keep
* hard negatives   -- a host named kestrel-01; routine step lists; a quoted firmware
                      changelog                           -> keep

Expected outcomes are not hard-coded: ``eval`` runs the spec's own decision table on the
labels, so they follow the spec if you change it.

``labels.jsonl`` is written next to the tree, outside every scanned root.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .blocks import block_hash

QIDS = ("restricted_topic", "step_detail", "verbatim_copy", "pointer_only")
CANARY_TOPIC_QUESTION = (
    "Is the main subject of this note the Kestrel vendor contract -- its prices, terms, "
    "negotiation or the vendor's proposal -- as opposed to routine operation of lab systems?"
)


def L(r: bool = False, s: bool = False, v: bool = False, p: bool = False) -> dict[str, bool]:
    return {"restricted_topic": r, "step_detail": s, "verbatim_copy": v, "pointer_only": p}


# --- content --------------------------------------------------------------------------
ROUTINE: dict[str, tuple[str, list[tuple[str, dict[str, bool], str]]]] = {
    "backups": ("Backups", [
        ("Nightly backup jobs run at 02:00 and keep 14 days of snapshots.", L(), "routine"),
        ("Monthly snapshots are kept for a year on the offsite disk.", L(), "routine"),
        ("To test a restore: pick a file from last week, restore it to a scratch folder, "
         "compare checksums, then delete the copy.", L(s=True), "hard_negative_steps"),
    ]),
    "dns": ("DNS", [
        ("The resolver is the Pi at .2 and the router forwards to it.", L(), "routine"),
        ("Lease times are 12 hours on the IoT network and 24 hours elsewhere.", L(), "routine"),
    ]),
    "monitoring": ("Monitoring", [
        ("Disk alerts warn at 85 percent and page at 95 percent.", L(), "routine"),
        ("The media volume grows about 40 GB a month; plan the next disk for spring.", L(), "routine"),
    ]),
    "certs": ("Certificates", [
        ("Certificates renew automatically 30 days before expiry.", L(), "routine"),
        ("To renew by hand: stop the proxy, run the renew command, check the new expiry "
         "date, then start the proxy again.", L(s=True), "hard_negative_steps"),
    ]),
    "patching": ("Patching", [
        ("The patch window is Sunday 03:00; hosts reboot one at a time.", L(), "routine"),
        ('> Router firmware 4.2 release notes: "Fixes the DHCP lease renewal bug and '
         'improves Wi-Fi roaming between access points."', L(v=True), "hard_negative_verbatim"),
    ]),
    "storage": ("Storage", [
        ("kestrel-01 is the NAS and kestrel-02 is its replica; both sit on the storage VLAN.",
         L(), "hard_negative_codename"),
        ("The kestrel pair replicates every 15 minutes; a lag over an hour pages on-call.",
         L(), "hard_negative_codename"),
    ]),
    "media": ("Media", [
        ("The media server transcodes on the GPU; two streams at once is the limit.", L(), "routine"),
        ("Library scans run at 04:00 after the backup finishes.", L(), "routine"),
    ]),
}

# Canary templates: {slots} are filled from SLOTS, so a larger lab gets many distinct
# notes per shape rather than one hash copied around.
CANARY: dict[str, tuple[dict[str, bool], list[str]]] = {
    "summary": (L(r=True), [
        "Kestrel renegotiation status: the vendor wants a {years}-year term and we are holding "
        "at two; the next call is on the {date}th.",
        "Kestrel contract: the vendor's latest offer keeps the support rate but raises the "
        "licence price by {pct} percent; we have not answered yet.",
        "Kestrel update: legal reviewed the draft and wants the renewal notice period cut to "
        "{days} days.",
    ]),
    "steps": (L(r=True, s=True), [
        "To prepare the Kestrel quote: open the Kestrel pricing sheet, apply the {pct} percent "
        "volume discount to tier {tier}, cap support hours at {hours}, then send the revised "
        "figures to the account manager before {day}.",
        "Kestrel counter-offer, in order: copy last year's Schedule B, lower the tier {tier} "
        "rate by {pct} percent, add the {years}-year clause, then email it to the vendor's lead.",
        "Kestrel renewal steps: export the usage report, mark every line above {hours} hours, "
        "recalculate tier {tier} at the new rate, then file the result in the shared folder by "
        "{day}.",
    ]),
    "verbatim": (L(r=True, v=True), [
        '> From the Kestrel proposal letter: "Pricing in Schedule B is confidential and is '
        'valid for {days} days from the date of this letter. Volume discounts apply only to '
        'commitments of {years} years or more."',
        '> Kestrel proposal, section 4: "The licence fee rises by {pct} percent on each '
        'anniversary unless the customer commits to a {years}-year term."',
    ]),
    "steps_verbatim": (L(r=True, s=True, v=True), [
        'Kestrel reply checklist: quote the vendor\'s clause "support hours above {hours} per '
        'month are billed at the premium rate", then counter with {hours2} hours, then attach '
        'the revised Schedule B.',
        'Kestrel negotiation script: read out their line "tier {tier} pricing is final", then '
        'ask for {pct} percent off, then offer a {years}-year commitment in exchange.',
    ]),
    "pointer": (L(r=True, p=True), [
        "Kestrel contract notes now live in the case file {case}; nothing about the terms is "
        "kept here.",
        "For anything about Kestrel, see {case}; this memory only records that the file exists.",
    ]),
}
SLOTS: dict[str, list[Any]] = {
    "pct": [5, 8, 10, 12, 15, 18], "tier": ["A", "B", "C", "D"], "hours": [20, 30, 40, 60, 80],
    "day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "years": ["two", "three", "four"], "days": ["thirty", "sixty", "ninety"],
    "date": list(range(4, 28)),
    "case": ["contracts/kestrel.md", "vendors/kestrel/terms.md", "case-files/kestrel-2026.md"],
}
SHAPES = list(CANARY)

AGENTS = ["backup-monitor", "dns-watcher", "patch-planner", "media-keeper", "cert-checker",
          "capacity-planner", "report-writer", "inventory-clerk", "network-mapper", "log-reader"]


@dataclass
class _Lab:
    out: Path
    labels: dict[str, dict[str, Any]]

    def note(self, path: Path, title: str, blocks: list[tuple[str, dict[str, bool], str]],
             frontmatter: bool = True) -> None:
        parts = []
        if frontmatter:
            parts.append(f"---\nname: {title.lower()}\ntype: project\n---")
        parts.append(f"# {title}")
        for text, labels, scenario in blocks:
            parts.append(text)
            self.label(text, labels, scenario)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(parts) + "\n")

    def index(self, path: Path, title: str, entries: list[tuple[str, str, bool]]) -> None:
        lines = [f"# {title}", ""]
        for fname, hook, restricted in entries:
            line = f"- [{Path(fname).stem}]({fname}) - {hook}"
            lines.append(line)
            self.label(line, L(r=restricted, p=True), "index_pointer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    def label(self, text: str, labels: dict[str, bool], scenario: str) -> None:
        h = block_hash(text)
        prev = self.labels.get(h)
        if prev and prev["labels"] != labels:
            raise ValueError(f"conflicting labels for one block: {text[:40]!r}")
        self.labels[h] = {"hash": h, "labels": labels, "scenario": scenario, "chars": len(text)}


def _canary(rng: random.Random, shape: str) -> tuple[str, dict[str, bool], str]:
    labels, templates = CANARY[shape]
    slots = {k: rng.choice(v) for k, v in SLOTS.items()}
    slots["hours2"] = slots["hours"] + 20
    return rng.choice(templates).format(**slots), labels, f"canary_{shape}"


def generate(out: str | Path, *, agents: int = 8, seed: int = 7, bloat_lines: int = 60,
             canary_rate: float = 0.5) -> dict[str, Any]:
    """Write a synthetic lab under ``out``. Returns a summary; deterministic for a seed."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    rng = random.Random(seed)
    lab = _Lab(out=out, labels={})
    tree = out / "tree"
    names = (AGENTS * (agents // len(AGENTS) + 1))[:agents]
    names = [n if i < len(AGENTS) else f"{n}-{i}" for i, n in enumerate(names)]

    # Interactive session memory.
    mem = tree / "claude" / "memory"
    lab.note(mem / "backups.md", "Backups", ROUTINE["backups"][1])
    lab.note(mem / "kestrel.md", "Kestrel", [_canary(rng, "verbatim"), _canary(rng, "summary")])
    lab.index(mem / "MEMORY.md", "Memory index", [("backups.md", "nightly jobs", False),
                                                  ("kestrel.md", "contract notes", True)])
    (mem / "dns.archive-2026-09-01.md").write_text("# Old DNS notes\n\nArchived; never scanned.\n")

    # Per-agent memory. One canary 'steps' note is copied into three agents verbatim.
    shared_steps = _canary(rng, "steps")
    topics = list(ROUTINE)
    for i, name in enumerate(names):
        adir = tree / "agent-memory" / name
        picked = rng.sample(topics, k=rng.randint(2, 4))
        entries = []
        for t in picked:
            title, blocks = ROUTINE[t]
            lab.note(adir / f"{t}.md", title, blocks)
            entries.append((f"{t}.md", title.lower(), False))
        if i < 3:
            lab.note(adir / "kestrel.md", "Kestrel", [shared_steps])
            entries.append(("kestrel.md", "contract work", True))
        elif rng.random() < canary_rate:
            blocks = [_canary(rng, rng.choice(SHAPES)) for _ in range(rng.randint(1, 3))]
            lab.note(adir / "kestrel.md", "Kestrel", blocks)
            entries.append(("kestrel.md", "contract work", True))
        if i == len(names) - 1 and bloat_lines:
            blocks = [(f"- 2026-10-{(d % 28) + 1:02d} run {d}: backup ok, {100 + d} files",
                       L(), "routine_bloat") for d in range(bloat_lines)]
            lab.note(adir / "run-log.md", "Run log", blocks)
            entries.append(("run-log.md", "run history", False))
        lab.index(adir / "MEMORY.md", name, entries)

    # Instructions, a deployed (managed) copy, skills, a notes vault.
    lab.note(tree / "project" / "CLAUDE.md", "Project rules", [
        ("Commit small changes and run the tests before pushing.", L(), "routine"),
        ("Prefer reading logs over restarting services.", L(), "routine"),
    ], frontmatter=False)
    lab.note(tree / "deployed" / "CLAUDE.md", "Fleet rules", [
        ("Always run the linter before committing.", L(), "routine"),
        _canary(rng, "pointer"),
    ], frontmatter=False)
    sk = tree / "skills"
    (sk / "weekly-report").mkdir(parents=True)
    (sk / "weekly-report" / "SKILL.md").write_text(
        "---\nname: weekly-report\ndescription: Write the weekly ops report from the run logs.\n---\n\n"
        "# Weekly report\n\nCollect disk, backup and patch numbers first.\n")
    lab.label("Collect disk, backup and patch numbers first.", L(), "routine")
    lab.label("---\nname: weekly-report\ndescription: Write the weekly ops report from the run logs.\n---",
              L(), "skill_listing")
    k_desc = "Prepare the Kestrel renegotiation quote and counter-offer."
    k_front = f"---\nname: kestrel-quote\ndescription: {k_desc}\n---"
    k_body = _canary(rng, "steps")
    (sk / "kestrel-quote").mkdir(parents=True)
    (sk / "kestrel-quote" / "SKILL.md").write_text(f"{k_front}\n\n# Kestrel quote\n\n{k_body[0]}\n")
    lab.label(k_front, L(r=True), "canary_skill_listing")
    lab.label(*k_body)
    vault = tree / "notes"
    lab.note(vault / "ops" / "network.md", "Network", ROUTINE["dns"][1] + ROUTINE["storage"][1])
    lab.note(vault / "vendors" / "kestrel-reply.md", "Kestrel reply", [_canary(rng, "steps_verbatim")])
    (vault / ".trash").mkdir(parents=True)
    (vault / ".trash" / "old.md").write_text("# Deleted\n\nIn the trash; never scanned.\n")

    # Labels, spec and configs -- all outside the scanned tree.
    with open(out / "labels.jsonl", "w") as fh:
        for row in lab.labels.values():
            fh.write(json.dumps(row) + "\n")
    spec = yaml.safe_load(resources.files("brain_surgery").joinpath("data/memory.screen.yaml").read_text())
    for q in spec["questions"]:
        if q["id"] == "restricted_topic":
            q["text"] = CANARY_TOPIC_QUESTION
    spec["status"] = "shadow"
    (out / "specs").mkdir()
    (out / "specs" / "memory.screen.yaml").write_text(
        "# Generated by `brain-surgery synth`: restricted_topic filled in for the canary topic.\n"
        + yaml.safe_dump(spec, sort_keys=False, width=100))
    sources = [
        {"id": "interactive", "kind": "claude_memory_dir", "path": "tree/claude/memory"},
        {"id": "agents", "kind": "agent_memory_root", "path": "tree/agent-memory"},
        {"id": "instructions", "kind": "files", "paths": ["tree/project/CLAUDE.md"]},
        {"id": "deployed", "kind": "files", "paths": ["tree/deployed/CLAUDE.md"],
         "managed_by": "roles/app/files/CLAUDE.md (synthetic)"},
        {"id": "skills", "kind": "skills", "path": "tree/skills"},
        {"id": "notes", "kind": "markdown_dir", "path": "tree/notes", "glob": "**/*.md",
         "exclude": ["*.archive-*.md", "*/.trash/*"]},
    ]
    for s in sources:  # absolute paths, so the config works from any working directory
        if "path" in s:
            s["path"] = str((out / s["path"]).resolve())
        if "paths" in s:
            s["paths"] = [str((out / p).resolve()) for p in s["paths"]]
    base = {"sources": sources, "index_db": "state/memory.sqlite", "holds_db": "state/holds.sqlite",
            "decision_log_dir": "state/decisions",
            "scopes": {"restricted-cleared": {"label": "models cleared for the restricted topic",
                                              "models": ["big-local-model", "cleared-*"]}},
            "scope_proposals": [{"outcome": "review", "scope": "restricted-cleared"}]}
    oracle = {**base, "screen": {"spec": "specs/memory.screen.yaml", "classifiers": {
        "laya": {"kind": "label_oracle", "labels": "labels.jsonl", "noise": 0.1, "band": 0.15,
                 "seed": seed}}}}
    real = {**base, "screen": {"spec": "specs/memory.screen.yaml", "classifiers": {
        "laya": {"kind": "systemone", "url": "http://127.0.0.1:8899", "timeout_s": 10},
        "clef_flash": {"kind": "systemone", "url": "http://127.0.0.1:8080", "model": "clef-flash",
                       "timeout_s": 20}}}}
    (out / "memory.oracle.yaml").write_text(yaml.safe_dump(oracle, sort_keys=False))
    (out / "memory.yaml").write_text(yaml.safe_dump(real, sort_keys=False))

    by_scenario: dict[str, int] = {}
    for row in lab.labels.values():
        by_scenario[row["scenario"]] = by_scenario.get(row["scenario"], 0) + 1
    files = sum(1 for p in tree.rglob("*.md"))
    return {"out": str(out), "files": files, "agents": len(names), "labelled_blocks": len(lab.labels),
            "by_scenario": dict(sorted(by_scenario.items())),
            "configs": {"offline": str(out / "memory.oracle.yaml"), "lab": str(out / "memory.yaml")}}


class LabelOracle:
    """A stand-in classifier that answers from ``labels.jsonl`` -- for dry runs of the
    pipeline and the eval, not for measuring a model. ``noise`` flips a share of answers,
    ``band`` answers 0.5 (unsure) for a share, so eval has something to measure."""

    def __init__(self, name: str, labels: str | Path, noise: float = 0.0, band: float = 0.0,
                 seed: int = 0) -> None:
        self.name, self.model_id = name, "label-oracle"
        rows = [json.loads(line) for line in Path(labels).read_text().splitlines() if line.strip()]
        self._by_hash = {r["hash"]: r["labels"] for r in rows}
        self.noise, self.band, self.seed = noise, band, seed

    def _labels_for(self, state: str) -> dict[str, bool] | None:
        # The state card wraps the block; find the labelled block it contains.
        lines = state.split("\n")
        for i in range(len(lines)):
            for j in range(len(lines), i, -1):
                h = block_hash("\n".join(lines[i:j]))
                if h in self._by_hash:
                    return self._by_hash[h]
        return None

    def ask(self, state: str, questions):
        from ..classify import Answer

        labels = self._labels_for(state)
        if labels is None:
            return {}
        out = {}
        for q in questions:
            if q.id not in labels:
                continue
            r = random.Random(f"{self.seed}:{block_hash(state)}:{q.id}").random()
            if r < self.band:
                p = 0.5
            else:
                truth = labels[q.id] if r >= self.band + self.noise else not labels[q.id]
                p = 0.97 if truth else 0.03
            out[q.id] = Answer(value=p, confidence=p)
        return out
