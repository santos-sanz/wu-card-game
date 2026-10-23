# Wu card game: comparing Decisions models and Text models

Goal: replicate with LLMs the card-game resolution Scott Wu (Cognition) did on the Stripe podcast *Cheeky Pint*, and compare two kinds of OpenRouter models on it: **Decisions** models (`output_modalities=decisions`) and **Text** models (the normal chat/completion kind). This document is a plan. Nothing is implemented or run yet.

## 1. Source and what is actually confirmed

- Episode: "A Cheeky Pint with Cognition CEO Scott Wu", https://www.youtube.com/watch?v=MmKkNmnoEvw (game in the first minute).
- Transcript used: https://podscan.fm/podcasts/cheeky-pint/episodes/cognition-ceo-scott-wu-on-acquiring-windsurf-ai-replacing-engineers-and-the-moneyball-ification-of-everything
- Source check: the game segment was read from the transcript. The video was opened, but the cards and rules on screen were not visually verified.

### 1.1 Reconstruction of the example (from the transcript only)

- Cards are shuffled and dealt. Scott has six cards and the target is 163.
- He says, roughly: 2 x 8 = 16; 9 / 3 = 3; 3 + 16 = 19; 12 x 12 = 144; 144 + 19 = 163. The card values 2, 8, 9, 3, 12, 12 are inferred from those steps.
- He adds that almost all combinations can be solved this way.

### 1.2 Not confirmed by the source

Deck composition, whether all six cards must be used, whether a card can be used more than once, whether intermediate results must be integers, whether the target changes per round, and how the game is scored. Anything below about those points is a **proposed rule for this experiment**, not Wu's rule.

## 2. The two model types (as defined by the author)

- **Decisions**: models listed at https://openrouter.ai/models?output_modalities=decisions
- **Normal**: models whose output modality is Text.

### 2.1 What a Decisions model is (OpenRouter docs, checked 2026-10-23)

- OpenRouter lists `decisions` as an output modality alongside text, image, audio, etc. (https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties). Text is the default filter, so Decisions models only show with `output_modalities=decisions`.
- They are called through a separate alpha endpoint, `POST https://openrouter.ai/api/alpha/decisions`, not chat completions (https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request).
- Request: a `model`, a `state` (string, or JSON object or array) and a `questions` map. Each question has a type: `noul` (yes/no, returns probability of yes), `choice` (pick one option from a set you define, returns a probability per option), `score` (position on an ordered scale you define). The Jev family also has `box` for images (https://openrouter.ai/docs/guides/community/jev).
- Response: typed answers with probabilities and confidence, not generated text. Jev docs state it produces no reasoning traces, explanations or free-form text.
- Models seen in the live catalog (https://openrouter.ai/api/v1/models?output_modalities=decisions): typesafe/jev-1.14, baseten/mammoth-1:free, baseten/elephant-1:free, perplexity/pplx-decider-v1.2, perplexity/pplx-decider-flash-v1.2, nace-ai/drex-max, respan/span-02, liquid/d1-ultra. The catalog changes, so re-list before running.

### 2.2 Consequence for the game

A Decisions model cannot write out "2*8=16, ...". It can only answer typed questions about a state. So a fair comparison needs the game phrased as typed questions, and the experiment needs an outer program (the harness) that does the arithmetic and search. This is the main design question, see section 4. Do not assume Decisions = reasoning model, and do not assume the Text side is turn-by-turn. Those are separate choices.

## 3. Game rules to test (proposed, not Wu's)

- Hand: 6 cards from a configurable deck (default: values 1 to 13; to be adjusted once the real deck is confirmed).
- Target: 163 by default, configurable.
- Operations: + - x /.
- Variant flags, each tested explicitly: `must_use_all`, `allow_reuse`, `integer_only_intermediates`.
- Sanity case: the podcast hand 2, 8, 9, 3, 12, 12 with target 163.

## 4. Experimental design

### 4.1 Ground truth
- Exact solver over all expression trees for 6 numbers. Gives solvable or not, and a reference solution, for every hand.
- Verifier consumes a list of steps. State is a multiset of operands (cards plus intermediate results). Each step removes two operands from the multiset and adds the result, so an intermediate result can be used once and cards cannot be reused unless `allow_reuse` is on. The final operand must equal the target, and with `must_use_all` the multiset must end with one item.

### 4.2 Condition A: Text models
- Prompt with hand and target, ask for the steps in a fixed JSON schema. Sub-variants kept as separate factors, not mixed into the Text/Decisions axis: one-shot full answer vs iterative turns; with or without extended reasoning enabled.

### 4.3 Condition B: Decisions models
Candidate framings, to pick before running (open design choice, to be decided by the author):
1. Step policy: the harness enumerates candidate pairs and operators, and the Decisions model answers `choice` or `score` questions ("which move is most promising?", "can the target still be reached from this state?") over a JSON state. The harness runs the search guided by those probabilities.
2. Value function: a `noul` or `score` question on a state ("is this state solvable?") used inside a search (beam, best-first).
3. Direct: one `noul` question per hand ("is 163 reachable?") to measure plain classification accuracy against the exact solver. This does not produce a solution.

Framings 1 and 2 need the harness to carry out the arithmetic, so results should be reported as "model + search harness", not as the model alone. Framing 3 is a pure classification benchmark.

### 4.4 Controls
- Same hands, seed and rules for every model.
- A harness-only baseline (random or greedy move choice with the same search budget), to show what the model adds.
- Log the exact request and response for each call.

## 5. Metrics
- Hands solved with a verified solution (where applicable).
- Solvable / unsolvable classification accuracy (all framings).
- Calibration of Decisions probabilities (for example Brier score).
- Invalid step rate and arithmetic error rate (Text).
- Model calls, tokens, latency and cost per hand. OpenRouter lists very low per-token prices for decisions models, to be verified at run time.

## 6. Dataset
- N hands (for example 500) from a fixed seed, labelled solvable or not and with the minimum cards needed.
- Include the podcast hand.

## 7. Suggested layout
```
wu-card-game/
  README.md
  PLAN.md
  game.py        # rules, dealing, exact solver, verifier
  prompts.py     # Text prompts
  decisions.py   # question schemas and state encoding for /api/alpha/decisions
  run.py         # runs models over the dataset
  data/hands.jsonl
  results/
```

## 8. Open questions
1. Which Decisions framing in 4.3 (or all three)?
2. Which Text models, and with or without reasoning enabled?
3. Real deck and rules from the video (must use all six cards, integer intermediates).
4. Budget cap for paid API calls before any run (nothing has been run).
