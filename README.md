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
changed since. **Excise** scrubs the text from the hold and watchlists the hash; every
later `scan` reports the block if it reappears anywhere (exit code 10).

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
```

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

- **Provenance:** for each block, find the session that wrote it (Claude Code session
  logs, git history), then what it was copied from, then repeat back to the root. Group the
  index by root, so one fix covers every copy.
- **Calibration:** turn quarantine, release and excise decisions into labelled data for
  the memory screen; refit thresholds; later, a model trained locally.
- **Write-time screen:** run the same spec as memories are written, and send a high
  scorer straight to quarantine (once calibration clears the promotion gate).
- **Scoping:** keep a memory but load it only into sessions on a designated model.
- **Review page** on the hold queue: prompts and memories together, with drill-down from
  source to agent to file to block.
