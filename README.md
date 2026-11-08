# brain-surgery

Two tools on one classifier cascade and one review queue:

- **Router.** Send each prompt to the **smallest configured model that can handle it**,
  decided by one fast "system one" classifier pass. Anything no configured model can run
  is **held** for human review.
- **Memory.** Index every agent memory store down to the block, find what is duplicated
  across agents, screen blocks in shadow, and **quarantine** individual blocks. A
  quarantined block can be released back into place or excised for good, with a watch
  for it growing back. See [Memory](#memory).

**Nothing prints memory or prompt text unless you pass `--text`.** An agent session
that runs these commands sees ids, paths, line ranges, hashes and scores, not the
content being reviewed.

## How it works

```
prompt ──► decision spec cascade ──► outcome ──► route ──► target 1 ─► target 2 ─► …
           (rule → laya → clef → …)  (small /     (ordered,   │ unavailable: try next
            all pending questions     medium /     cheapest   │ declined:    HOLD
            in ONE call per tier)     large /      first)     │ completed:   done
                                      unsure)                 └ none left:   HOLD
```

1. **Decide.** A [decision spec](#decision-specs) lists atomic typed questions
   ("does this need several dependent steps?", "does it ask for code to be written?") and
   a fixed decision table. Tiers are asked in order; each tier gets **every
   still-unresolved question in a single call**. A question resolves at the first tier
   whose answer clears that tier's threshold. The table is evaluated after each tier
   and the cascade stops as soon as it can decide.
2. **Route.** `routes` maps every outcome to an ordered list of targets, cheapest
   first, or to `hold`.
3. **Dispatch.** Targets are tried in order:
   - an **unavailable** target (connection error, rate limit, 5xx) moves to the next;
   - a **declined** request stops and goes to the hold queue. It is not retried on
     the route's other targets;
   - if nothing in the route was available, the request is held.

### Rules that hold under failure

- **Errors escalate, never guess.** A tier that times out or returns garbage
  contributes nothing. If tiers run out with the table still undecided, the outcome is
  the spec's `unsure`. That is routed like any other outcome, usually to the larger
  models.
- **First match wins, three-valued.** A table row is TRUE, FALSE or UNKNOWN. An UNKNOWN
  row above a TRUE one blocks the decision, so a looser later row can never fire just
  because the classifier that would have answered an earlier, stricter row is down.
- **No threshold, no confidence.** A question without a calibrated `hi` for a tier
  never resolves at that tier.
- **Local-only routes stay local.** A route with `data_tier: local-only` cannot list a
  cloud target; config loading fails if it does.

## Classifiers

Anything implementing `ask(state, questions) -> {qid: Answer}` can be a tier:

| kind | what |
|---|---|
| `systemone` | any server speaking `POST /v1/systemone` (Laya, Clef, or your own model behind the same API) |
| `rules` | deterministic regexes; a match answers with certainty, no match abstains |
| `static` | fixed answers, for dry runs, tests and replaying logged answers |

Wire format (the request and response bodies Laya and Clef share):

```json
{"model": "optional", "state": "<rendered state card>",
 "questions": {"multi_step": {"type": "noul", "instructions": "..."},
               "kind": {"type": "choice", "instructions": "...", "criteria": {"a": "...", "b": "..."}}}}
→ {"answers": {"multi_step": {"noul": 0.91},
               "kind": {"choice": "a", "probabilities": {"a": 0.8, "b": 0.2}}}}
```

## Targets

| kind | what |
|---|---|
| `openai` | any OpenAI-compatible `/v1/chat/completions` (LM Studio, vLLM, llama.cpp, Ollama, LiteLLM). `finish_reason: content_filter` counts as declined. |
| `anthropic` | the Claude API through the official `anthropic` SDK (`pip install .[anthropic]`). `stop_reason: refusal` counts as declined; the category from `stop_details` is recorded on the hold. |

**Anthropic server-side fallback is on by default** for models that support it
(`claude-opus-5-5`, `claude-opus-5`, `claude-fable-5-1`, `claude-sonnet-5-5`):
`fallbacks: "default"` with beta `server-side-fallback-2026-07-01`. On a decline, the API
itself re-runs the request on the model Anthropic designates for that refusal category,
and `Completion.served_by` shows which model answered. The router only sees a refusal
if that whole chain declined. Set `server_fallback: off` on the target to disable it.
Credentials come from `api_key_env` if set, otherwise from the SDK's normal chain. Point
each target at the API key of the workspace whose access programme should apply.

## Decision specs

Specs use the same YAML shape as the homelab's `decisions/specs/_template.yaml`.
Keys this engine doesn't use (`mask`, `variants`, `labels`, `results`, `inputs`,
`max_len`, …) are accepted and ignored, so one file serves both. Validation is fatal and
reports every problem at once (`brain-surgery lint`). It requires:

- `error_direction`: `noise_gate` or `safety_gate`;
- `on_error: escalate`;
- an `unsure` outcome with an `escalate_to`;
- a final `default` row;
- declared outcomes only;
- question types `noul`, `choice` or `score`, and no more than 14 choice options.

A `safety_gate` may not produce a permissive outcome from model answers alone; that row
also needs a deterministic fact.

Table conditions:

| on | condition |
|---|---|
| `noul` / `score` question | `">=hi"`, `"<=lo"`, `"unsure"` |
| `choice` question | a label, or `"unsure"` |
| a fact (any other key) | a literal (`true`, `"x"`) or a comparison (`">24000"`, `"<=3"`) |

A missing fact never matches.

Every routing decision gets `prompt_chars` and `prompt_lines` as facts automatically;
callers can pass more.

See `examples/specs/route.prompt_size.yaml`. Its thresholds are **starting guesses**:
run it in shadow, label, and refit before trusting them.

## Decision log

With `decision_log_dir` set, every decision appends JSONL to one file per UTC day:
one row per question per tier, in the `decisions.cascade_events` shape, plus a summary
row with the route and dispatch result. That's the calibration set and the replay set
for swapping classifiers. Writing the log never raises into the caller.

## Hold queue

`holds_db` is SQLite. Held prompts store their text, facts, route and every attempt;
quarantined memory blocks store their text and position. Both follow one lifecycle:
`held → released | excised | dismissed`. Point `routing.yaml` and `memory.yaml` at the
same file to review both in one queue. Keep it on local disk; it holds full text.

## Memory

```
sources ──scan──► index (every block, keyed by content hash)
                    │
                    ├─ tree / dupes / blocks     what exists, where, how often loaded
                    ├─ screen (shadow)           spec outcome per hash, recorded only
                    └─ quarantine ──► hold ──┬─ release   back where it was
                                             └─ excise    text scrubbed, hash watchlisted
                                                           → scan reports any regrowth
```

**Sources** (`memory.yaml`):

| kind | what | loaded |
|---|---|---|
| `claude_memory_dir` | one Claude Code memory folder | `MEMORY.md` always, topic files on demand |
| `agent_memory_root` | a folder of per-agent memory folders (`memory: project` subagents) | same, per agent |
| `files` | explicit files / globs, e.g. CLAUDE.md | always (configurable) |
| `skills` | `*/SKILL.md` | front-matter always (it is listed), body on demand |
| `markdown_dir` | any markdown tree, e.g. a notes vault | on demand (configurable) |

`loaded` becomes an **exposure** weight (3 always, 1 on demand, 0 never). An always-loaded
block is in every session; that's where a problem block costs most.

A source can carry `managed_by:` for a **deployed or generated copy**, such as a
CLAUDE.md that Ansible copies from a repo. Quarantine refuses to edit it and names the real
source instead, because the next deploy would put the content straight back. `--force`
overrides.

**Blocks** are frontmatter, headings, list items (with continuation lines), table rows,
fenced code and paragraphs. A block is identified by its content hash and occurrence
number, not its line number. Agents keep writing to these files, and line numbers
drift between a scan and a quarantine; the hash is what gets matched.

**Quarantine** takes the chosen blocks out of their files, so no agent loads them, and
holds them with their position: the hashes of the blocks either side. Writes are
compare-and-swap: the file is re-read before writing, and nothing is written if an agent
changed it in between. The new content goes to a temporary file that is then renamed
into place. **Release** puts a block back between its old neighbours, even if the file has
changed since. While a block is quarantined, `scan` reports it if it reappears in the file
it came from: typically an agent saving back a copy it read before the quarantine.
**Excise** scrubs the text from the hold and watchlists the hash; every later `scan`
reports the block if it reappears anywhere. Any regrowth makes `scan` exit with code 10.

A held block then leaves the queue one of three ways:

* **release** — restore the original wording, verbatim, between its old neighbours.
* **release reworded** — restore an *edited* version instead (`release --replace FILE`, or the
  reword panel in the web Holds drawer). Use it to **clarify** a false positive or to
  **summarize to a pointer** — "analysis done; details in the case file on the local tier" —
  which keeps what the agent needs (that the work happened, and where it lives) and drops the
  detail that didn't belong in always-loaded memory. The original wording is watchlisted, so a
  later scan reports it if an agent writes it back.
* **excise** — remove it for good; the text is scrubbed and the hash watchlisted everywhere.

Before releasing, **`rescreen`** previews what the screen would say about the held text or a
reword (`memory rescreen HOLD [--replace FILE]`, or "re-screen" in the panel) — it runs the
same cascade and **records nothing**. This checks the block against *your own local memory
gate* (Laya/Clef), not any provider's safety classifier; it is for confirming a false positive
reads as benign, or that a pointer-only rewrite no longer carries the detail — not for tuning
sensitive content to slip past a safety system, which is evasion and stops working the moment
the classifier changes. If a memory genuinely needs its full detail, keep it on the local tier
(and route the work that needs it to a permitted model) rather than rewording to hide it.

**`scan` / `rescan`** re-indexes the files after you or an agent change them — it is how a fix
at the *source* (editing a root file, then rescanning) shows up, and how regrowth is detected.
It never touches holds.

**Shadow screen** runs a decision spec (`examples/specs/memory.screen.yaml`) over every
block not yet screened, once per distinct hash, so identical copies across agents cost
one decision. It **only records** outcomes; nothing is quarantined automatically. Your
quarantine, release and excise calls are the labels for calibrating it. The decision
log records hashes and block ids, never the text. The example spec's `restricted_topic`
question is a placeholder: write your own topics into it, concretely.

State files (`index_db`, `holds_db`, `decision_log_dir`) must sit outside every scanned
root; loading the config fails otherwise, so quarantined text can't be scanned back in.

```bash
M="brain-surgery memory --config examples/memory.yaml"
$M scan                                   # index; exit 10 if excised content grew back
$M tree [--agent A] [--source S]          # files, exposure, block counts, flagged
$M dupes                                  # blocks held identically in several files
$M blocks --file PATH | --contains STR | --hash H | --outcome quarantine   [--text]
$M quarantine --id ID... | --file PATH | --hash H  --reason "why"   [--force]
$M release HOLD_ID                        # put it back
$M excise HOLD_ID --note "confirmed"      # scrub + watch
$M screen [--limit N] | --summary         # shadow screen
$M eval --labels labels.jsonl             # grade the screen against known labels
```

## Synthetic lab

Test and calibrate on memory that is realistic in shape but harmless in content:

```bash
brain-surgery synth --out /tmp/lab --agents 40 [--seed 7] [--canary-rate 0.5]
M="brain-surgery memory --config /tmp/lab/memory.oracle.yaml"   # or memory.yaml for real Laya/Clef
$M scan && $M dupes && $M screen && $M eval --labels /tmp/lab/labels.jsonl
```

It writes a full tree: an interactive memory folder with its index, per-agent memory
folders, CLAUDE.md, a **managed** deployed copy, skills, a notes vault, an archive file and
a trash folder (both excluded), plus one large run log. Every content block has a known
label in `labels.jsonl`, which sits outside the tree.

The restricted topic is a **canary**: a fictional vendor contract, "Kestrel". It stands in
for whatever your policy keeps out of always-loaded memory, so the pipeline can be measured
without generating anything genuinely sensitive. The canary comes in every shape the spec
distinguishes, each with templated variants so a large lab has many distinct notes:

| scenario | labels | expected |
|---|---|---|
| `canary_summary` | restricted | review |
| `canary_steps` (one copy shared by three agents) | restricted + step detail | quarantine |
| `canary_verbatim` | restricted + word-for-word quote | quarantine |
| `canary_steps_verbatim` | restricted + both | quarantine |
| `canary_pointer` | restricted, points elsewhere only | keep |
| `hard_negative_codename` | a host named `kestrel-01` | keep |
| `hard_negative_steps` / `_verbatim` | routine step lists, a quoted firmware changelog | keep |

The "expected" column isn't stored anywhere: `eval` runs the spec's own decision table
on the labels, so it follows the spec if you change it.

Two generated configs:

- **`memory.oracle.yaml`** uses a `label_oracle` classifier, which answers from the labels
  with tunable `noise` (wrong answers) and `band` (unsure answers). It is a dry run of the
  plumbing and the metrics, not a measurement of any model.
- **`memory.yaml`** points the same spec at `/v1/systemone` endpoints (Laya, Clef). Run it in
  the lab to measure the real classifiers on the canary.

`eval` reports what the lab grades classifiers by: **precision when confident** and
**coverage** per question and tier, the **escalation** rate per question, the precision of
decided outcomes, the unsure rate, an expected-vs-actual confusion table and a
per-scenario breakdown. If the screen ran without `decision_log_dir`, only the outcome
metrics are available.

## Write-time screening (auto-quarantine)

The proactive twin of the batch screen: screen a memory **as it is written**, and — in
enforce mode — auto-quarantine a high scorer before the next session ever loads it.

```yaml
# memory.yaml
write_screen:
  mode: shadow            # shadow (record only) | enforce (auto-quarantine)
  actions: [quarantine]   # which outcomes auto-quarantine in enforce mode
```

```bash
brain-surgery memory --config memory.yaml screen-write --file PATH…   [--enforce | --shadow]
```

It re-indexes the written file, screens its new blocks, records and logs each outcome, and
in enforce mode quarantines any whose outcome is in `actions`. It is a **safety gate**: it
can only move a block to the review queue, never release or excise. It will not touch a
**managed** copy (a deploy would restore it) or content already under review or excised
(watchlisted), and it reports those as skipped.

**Default is shadow, and that matters.** Run it in shadow so it records what it *would*
quarantine without acting; grade those decisions with `eval` against your own
release/excise labels; flip to `enforce` only once it clears your precision/escalation bar.
Promoting on the day you first switch it on is the mistake the register warns about — let it
operate and catch (or clear) real cases first.

**Two wirings, both yours** (this session can't touch your fleet):

- **Interactive Claude Code sessions** — a `PostToolUse` hook on Write/Edit. A ready example
  is `examples/hooks/post_write_screen.py`: it acts only on files under a scanned memory
  root, and never fails the agent's write on a screening error.
- **The production fleet** (which runs no hooks) — call `memory screen-write --file <path>`,
  or import `brain_surgery.memory.write_screen.screen_write`, from the orchestrator's
  dispatch step after an agent run changes a memory file.

The review page's header shows the current write-screen mode.

## Review page

A self-hosted page over the index and hold queue: see where risky memory lives, drill in,
select blocks across agents, and quarantine/release/excise — the visual front end to
everything above.

```bash
brain-surgery memory --config memory.yaml web [--host 127.0.0.1] [--port 8795] [--read-only]
```

- **Risk band** — the flagged groups (quarantine/review), cross-agent duplicates and
  regrowth, each as a card with a sample, copy count and agents. Click one to drop every
  copy across agents into the staging tray.
- **Treemap** — source → agent → file, sized by bytes, colored by worst risk; click to
  zoom, breadcrumb to go back, click a file to open it. A ⚙ marks a managed copy.
- **File view** — the blocks with a risk gutter; click to select, shift-click for a range,
  "select flagged" for all; each block has a **trace** link to its provenance.
- **Staging tray** — the current selection; add a reason and quarantine (with a force
  toggle for managed copies).
- **Holds drawer** — held memories with release / excise (excise confirms); filter by status.
- **Roots** — flagged blocks grouped by the source they trace to, each with its fix.

It is the one place that shows memory text (it is the review surface). It **binds to
127.0.0.1 and has no auth of its own** — put it behind your proxy and login (Caddy +
Authentik on the reference fleet); it can read every agent's memory and, without
`--read-only`, change it. `--read-only` disables quarantine, release, excise and rescan.

## Scoping — keep a memory, load it only on the right model

A memory can be legitimate yet trip a general model's safety classifier during ordinary
work. Instead of deleting it, **scope** it to a model: the block moves out of its live file
into a sibling `…​.scoped.<scope>.md` that the agent's normal memory glob excludes, and a
**loader** concatenates it back only in sessions on a model the scope permits. In a
general-model session the block is simply absent (nothing to trip); on a cleared model it
loads as before. Reversible with `unscope`.

```yaml
# memory.yaml
scopes:
  restricted-cleared:
    label: "models cleared for this topic"
    models: ["claude-opus-4-7", "claude-opus-4-8"]   # exact ids or fnmatch globs
  local-only:
    models: ["qwen*", "clef*"]
scope_proposals:                       # the screen proposes; you confirm
  - {outcome: review, scope: restricted-cleared}
```

```bash
M="brain-surgery memory --config memory.yaml"
$M propose-scopes                      # blocks the screen suggests scoping
$M scope --id ID… | --file F | --hash H --scope restricted-cleared
$M unscope SCOPED_ID                   # move it back to the original file
$M scoped [--scope NAME]               # what is scoped
$M scoped-for --model MODEL [--agent A] [--text | --paths]   # the loader
```

**Wiring (two steps, both yours):**

1. **Exclude the scoped files from the agents' own memory.** brain-surgery's scan already
   skips `*.scoped.*.md`; make the *fleet's* memory loading skip them too, so a scoped block
   doesn't leak back into general sessions. (If agents read a whole directory, the sibling
   sits beside the original — exclude the `.scoped.` pattern there.)
2. **Load the scoped files when the model matches.** At session start, run
   `scoped-for --model <the session's model> --agent <agent> --text` and concatenate the
   output into that agent's memory. A general model matches no scope and gets nothing; a
   cleared model gets its scoped blocks back. Wire it into a Claude Code `SessionStart` hook,
   or into the orchestrator's dispatch step (where the model is already chosen).

Scoping pairs with the router: the same model table decides where a prompt runs and which
memories a session on that model may load. It is **not** a way to hide content from a
provider's safety system — it keeps a genuinely-cleared memory out of sessions that aren't
cleared for it, and the honest path for work that needs both the detail and a capable cloud
model is a verified workspace (CVP), not scoping around the classifier.

## Provenance

Trace a memory block back to what it was copied from, to a root you can act on, so one fix
covers every copy instead of deleting block by block.

```bash
M="brain-surgery memory --config memory.yaml"
$M trace --id BLOCK_ID          # one block's chain to its root (metadata; --text adds the block)
$M roots [--all]                # group blocks by the root they trace to (default: review+quarantine)
$M trace-eval --truth FILE      # grade roots against a truth file (from `synth --with-logs`)
```

Add a `provenance:` block to `memory.yaml` pointing `logs:` at your Claude Code session-log
roots (dirs of `*.jsonl`). The reader pulls two things from each session: **writes** (a
Write/Edit, with the file's content after the change) and **inputs** (what the session read
first: file reads, web fetches, tool results, the CLAUDE.md / skills loaded as instructions,
user messages). For a block it finds the earliest write whose file contained it (who wrote
it, when, which agent), then the input that session read beforehand whose text best contains
the block (what it came from), then repeats to a root:

| root kind | means | what to do |
|---|---|---|
| `authored` | copied from a file no agent wrote (a register, a hand-written doc) | edit the source, regenerate/redeploy if generated, then quarantine the copies |
| `managed` | a deployed/generated copy (`managed_by`), or loaded from a CLAUDE.md/skill | change it at the named source; a deploy would restore it |
| `external` | from a web fetch or other outside feed | can't delete at source; filter where the feed enters |
| `user` | from a chat message | quarantine and watchlist |
| `composed` | the agent wrote it with no copied source found | quarantine; consider the instruction that makes the agent record it |
| `unknown` | no session log covers it (logs expired) | quarantine and watch, or widen the log window |

Matching is whitespace-normalized substring first (an exact copy, confidence 1.0), then
token-shingle overlap (a paraphrase, lower confidence) -- local only, no model. A
`managed_by` memory file is rooted at its source with no logs needed. With `git: true`, a
block with no session log falls back to `git log` on its file for a commit/author hint.

**Session logs expire** (Claude Code keeps them for a limited window), so start archiving
them now if you want provenance to reach back; the trace of an old block without logs reads
`unknown`. The provenance index stores log-derived text: keep it local, outside every
scanned root (checked on load).

## Use

```bash
uv venv && uv pip install -e '.[dev]'

brain-surgery lint examples/specs/*.yaml
brain-surgery route --config examples/routing.yaml --text "Summarise the backup report."   # decide only
brain-surgery route --config examples/routing.yaml --file prompt.txt --dispatch             # decide + run
brain-surgery holds --db examples/state/holds.sqlite list
brain-surgery holds --db examples/state/holds.sqlite resolve 3 released --note "ran on large local"
brain-surgery serve --config examples/routing.yaml --port 8790     # POST /v1/route, /v1/dispatch; GET /healthz
```

The HTTP front binds to `127.0.0.1` and has no authentication of its own; put it
behind your proxy and login before exposing it.

Library:

```python
from brain_surgery.router import Router
r = Router.from_config("examples/routing.yaml")
r.decide("Rename the backup job.").route.targets    # e.g. ['local-small', 'local-large', 'cloud']
res = r.dispatch("Rename the backup job.")         # res.status: completed | held
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Everything runs against in-process fake servers, a fake SDK client and a made-up memory
tree, using harmless sample text. No network and no real model calls.

## Roadmap

- **Calibration:** turn quarantine, release and excise decisions into labelled data for
  the memory screen; refit thresholds; later, a model trained locally.
