# brain-surgery

Route each prompt to the **smallest configured model that can handle it**, using one
fast "system one" classifier pass, and **hold** anything no configured model can run
for human review.

This is the first piece of a larger tool. The same classifier contract, decision specs
and review queue will drive the memory side next: scanning agent memories, tracing them
to their source, and quarantining them (see [Roadmap](#roadmap)).

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

`holds_db` is SQLite. Held prompts store their text, facts, route and every attempt.
Statuses follow one lifecycle shared with memories later:
`held → released | excised | dismissed`. Keep the database on local disk; it holds
the full prompt text.

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

Everything runs against in-process fake servers and a fake SDK client, using harmless
sample prompts. No network and no real model calls.

## Roadmap

- **Memory side:** source adapters (Claude Code memory folders, CLAUDE.md, skills, agent
  memory, markdown vaults), a line-level index keyed by content hash, a quarantine
  lifecycle on the shared hold queue, and provenance tracing back to the source of each
  memory.
- **Memory screen spec** run in shadow on the same cascade; labels from
  quarantine/release decisions feed calibration and, later, a locally trained model.
- **Scoping:** keep a memory but load it only into sessions on a designated model.
- **Review page** on top of the hold queue (prompts and memories together).
