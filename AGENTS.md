# Nimble Reviewer — project context for code agents

## What this is

A self-hosted GitLab bot that reviews merge requests using a two-model council: Codex and Claude run independent reviews in parallel, then a synthesis model merges results into a single MR comment.

## Architecture

```
GitLab webhook
      │
  app.py          ← FastAPI, validates token, enqueues run
      │
  store.py        ← SQLite: run queue, MR state, note IDs
      │
  worker.py       ← thread pool, polls queue, calls service
      │
  service.py      ← main orchestration: checkout → review → compare → post
    ├── gitops.py         checkout, diff, merge-base
    ├── prompts.py        build prompt JSON for agents
    ├── review_agent.py   CouncilRunner (Codex + Claude in parallel → synthesis)
    ├── finding_match.py  fuzzy dedup across runs (title overlap + line proximity)
    ├── renderer.py       ReviewResult → GitLab Markdown
    └── gitlab.py         create/update MR note
```

Supporting modules: `models.py` (frozen dataclasses), `config.py` (env vars), `trace.py` (JSONL run traces), `runtime_state.py` (review CLI symlinks and auth setup).

## Key data flow

1. Webhook arrives → `webhook.py` filters for open/reopen/draft-to-ready, skips push events
2. Run enqueued in SQLite; newer SHA on same MR supersedes the previous run
3. Worker claims run, service checks if SHA is still current (skips stale runs)
4. `gitops.py` clones mirror, checks out source SHA, extracts diff + changed files
5. `prompts.py` builds structured prompt (diff + metadata + optional `NIMBLE-REVIEWER.MD`)
6. `CouncilRunner` runs Codex and Claude in parallel, then synthesis model merges findings
7. Findings compared against previous run via `finding_match.py` → new / still_present / resolved
8. `renderer.py` formats result; `gitlab.py` creates or updates the single MR note

## Finding severity and rendering

Severity comes from the model: `high`, `medium`, `low`. Rendering tier is computed from severity + how many models found it:

| sources    | low       | medium   | high     |
|------------|-----------|----------|----------|
| 1 model    | short     | standard | detailed |
| 2 models   | short     | detailed | detailed |

- **short** — heading + source + status only (no body)
- **standard** — + body + opinions + snippet + suggestion
- **detailed** — same as standard

Finding label format: `🚨 High (Claude + Codex)`, `⚠️ Medium (Claude)`, `💡 Low (Codex)`.

## Important invariants

- Each MR gets exactly one bot note (create on first run, update on subsequent)
- Only one SHA per MR is active at a time; new push supersedes the queued/running job
- Push events to an already-open MR do **not** auto-trigger a review (by design)
- `finding_match.py` deduplication is fuzzy — tuning thresholds affects false positives
- Review traces are JSONL files in `REVIEW_TRACE_DIR`; the MR note intentionally stays short

## Running tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest
pytest tests/
```
