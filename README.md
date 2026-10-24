# Wu card game: Decisions vs Text race

A runnable benchmark for the card game from the Scott Wu / Cheeky Pint episode, built to compare two kinds of OpenRouter models on **time to a verified solution**:

- **Text lane** — a normal chat model through the Nam gateway. It gets the hand and target and must return the complete step list as JSON.
- **Decisions lane** — a `output_modalities=decisions` model through OpenRouter's alpha Decisions API. It never writes arithmetic. The harness enumerates every legal move, asks the model to rank them with a `choice` question, and executes a model-guided depth-first search with exact reachability pruning.

The repo contains the exact solver, verifier, dataset generator, CLI benchmark, FastAPI backend, and a browser control center that starts a race and shows both solutions live.

See [`PLAN.md`](PLAN.md) for the experiment design and the reconstruction of the original example.

## Quick start

```bash
cp .env.example .env        # fill in NAN_API_KEY and OPENROUTER_API_KEY
uv sync
uv run wu make-dataset --count 30
uv run wu serve             # http://127.0.0.1:8000
```

The control center checks both keys on load. If the OpenRouter key is missing or expired, the decisions lane will surface the provider error; the built-in `harness/heuristic` option lets you exercise the same lane without the API.

## What the races measure

- Both lanes start at the same time on the same hand and rules.
- A lane wins only when its solution passes the verifier.
- The clock stops at the first verified solution.
- The UI shows each lane's steps, final expression, calls, tokens, cost, live search path, and trace.
- Each lane has an animated poker-table view: cards lift and merge with an equation chip, results become foil cards, and the final target card turns green. The **Replay cards** button re-runs the animation on a completed race.
- Exact request/response payloads are saved to `results/races/<id>.json`; the polling API strips them to keep the UI light.
- Optional **baseline lane** uses the same guided search and pruning with a solver-free heuristic instead of a model, isolating what the model's ordering adds.

### Rules used by default

- Six cards, target `163`.
- Operators `+ - * /`.
- Every card used exactly once.
- Every intermediate result must be an integer and division must be exact.

The podcast hand `2, 8, 9, 3, 12, 12 → 163` is included as the first dataset record and as a preset in the UI.

### Decisions framing

The decisions lane implements **framing 1** from the plan with a reliability layer:

1. The harness enumerates every legal pair/operator move for the current state.
2. The Decisions model receives one `choice` question with all those moves and returns a probability for each.
3. The harness tries moves in the model's probability order.
4. The exact solver is used **only as a boolean prune oracle** ("is this successor still solvable?"). A solvable successor is explored; a dead end is skipped.
5. The first verified path wins. A solvable hand always produces a solution, even when the model's first move is a dead end.

The harness performs all arithmetic and pruning; the model controls move ordering. Results are **model + harness**, not the model alone. The lane reports expansions, pruned moves and solver checks so the model's ordering contribution is visible.

The text lane is the **one-shot full answer** variant: one JSON solution per attempt. It defaults to two attempts and an 8,192-token completion budget so reasoning models have room for both their reasoning and the JSON answer. Retries count toward elapsed time and are reported. If the final content is empty, the runner also tries to extract JSON from the reasoning trace.

## Presentation / recording mode

Click **Present** in the top bar, press **F**, or open the control center with `?present=1` (append `&race=<id>` to replay a saved race).

- Hides the setup sidebar, event log, archive and comparison panel.
- Enlarges the race header, clocks and poker-card tables; the two lanes fit a 1920×1080 canvas, with a compact scale for 900p/720p.
- The slim top bar keeps **Start race**, **Replay cards**, **Fullscreen** and **Exit** available.
- **Fullscreen** requests the browser Fullscreen API; **Esc**, **F** or **Exit** returns.

For OBS or a browser source, use `http://127.0.0.1:8000/?present=1` and press Fullscreen once.

## CLI

```bash
# exact solver on one hand
uv run wu solve 2,8,9,3,12,12 --target 163

# reproducible dataset, labelled by the exact solver
uv run wu make-dataset --count 100 --seed 7
uv run wu make-dataset --count 100 --seed 7 --include-unsolvable

# aggregate speed benchmark, both lanes
uv run wu benchmark --limit 10 \
  --text-model deepseek-v4-flash \
  --decisions-model baseten/mammoth-1:free \
  --text-max-attempts 2 --max-expansions 120

# repeated pass/fail check on a solvable hand
uv run wu stress --runs 5 \
  --text-model deepseek-v4-flash \
  --decisions-model baseten/mammoth-1:free

# include the harness-only control
uv run wu benchmark --limit 10 --include-baseline

# exercise the decisions runner without the OpenRouter API
uv run wu benchmark --limit 3 --decisions-model harness/heuristic
```

Each benchmark writes `results/benchmark-<timestamp>.json` and `.md` with per-hand results and aggregates (solved rate, median/mean/p95 time to solution, calls, tokens, cost).

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/bootstrap` | key status, model catalogues, defaults, dataset count |
| `POST` | `/api/analyze` | exact solver ground truth for a hand |
| `GET` | `/api/hands/random?target=163&solvable=true` | deal a labelled hand |
| `POST` | `/api/races` | create and start a race |
| `GET` | `/api/races/{id}` | current race state (live polling) |
| `POST` | `/api/races/{id}/cancel` | cancel a running race |
| `GET` | `/api/races` | recent race summaries |
| `GET` | `/api/export/{id}` | full saved race log |

## Layout

```
wu/
  game.py        exact solver, verifier, move generation, dataset helpers
  prompts.py     text prompt/schema and Decisions request builder
  clients.py     Nam chat client, OpenRouter Decisions client, key/model discovery
  runners.py     text runner, decisions runner, heuristic baseline
  race.py        race orchestration and persistence
  server.py      FastAPI app + static control center
  benchmark.py   aggregate CLI benchmark
  dataset.py     JSONL dataset builder
  cli.py         `wu` command
web/             control center (vanilla HTML/CSS/JS)
tests/           solver, verifier, prompts, decisions and full-race tests
data/hands.jsonl generated labelled hands
results/         benchmarks and raw race logs (git-ignored)
```

## Tests

```bash
uv run pytest
```

The suite covers the podcast hand, arithmetic-error detection, card reuse, reversed operands, integer-only intermediates, prompt parsing, the Decisions request shape, an oracle-driven decisions lane, and a full race with a fake text model.

## Reliability check

Run after the guided-search change (OpenRouter `baseten/mammoth-1:free`, Nam `deepseek-v4-flash`):

- 20/20 solvable hands solved by the text lane (median 6.59 s, p95 21.4 s, one 63 s outlier that still verified).
- 20/20 solved by the Decisions lane (median 1.64 s, p95 1.87 s, 5 model calls per hand).
- `uv run wu stress --runs 5`: 5/5 and 5/5, `STRESS PASS`.

## Known limitations

- The OpenRouter Decisions API is alpha and its catalogue/format can change.
- The exact solver does not implement `allow_reuse`.
- A Decisions lane cannot produce arithmetic; the harness does it. Do not report those results as pure model accuracy.
- The guided Decisions search uses the exact solver as a prune oracle. That makes the lane reliable, but it is a **model + harness** result, not a pure model score. The harness-only baseline uses the same search and pruning.
- The text lane's one-shot JSON response is not a multi-turn agent loop. That is a separate factor in the plan.
