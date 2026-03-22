# Nimble Reviewer — project context for code agents

## What this is

A self-hosted GitLab bot that reviews merge requests using a two-model council: Codex and Claude run independent reviews in parallel, then a synthesis model merges results into one review result. That result is published as a short summary note plus inline GitLab discussions where possible.

The bot also reacts to merge request note events with a lightweight discussion reconciliation pass so it can read human replies, reply in relevant threads, and dismiss bot findings as `dismissed_by_discussion` when the discussion convincingly resolves the concern.

## Architecture

```text
GitLab webhook
      │
  app.py          ← FastAPI, validates token, enqueues full-review or reconcile runs
      │
  store.py        ← SQLite: run queue, MR state, tracked finding lifecycle
      │
  worker.py       ← thread pool, polls queue, calls service
      │
  service.py      ← orchestration: checkout → review/reconcile → publish summary + discussions
    ├── gitops.py         checkout, diff, merge-base
    ├── prompts.py        build review and discussion-reconcile prompts
    ├── review_agent.py   CouncilRunner (Codex + Claude in parallel → synthesis)
    ├── finding_match.py  fuzzy dedup across runs and discussions
    ├── diff_mapping.py   unified diff → GitLab inline position mapping
    ├── renderer.py       summary note rendering
    └── gitlab.py         MR notes, discussions, diff versions, resolve/reply APIs
```

Supporting modules: `models.py` (frozen dataclasses), `config.py` (env vars), `trace.py` (JSONL run traces), `runtime_state.py` (review CLI symlinks and auth setup).

## Key data flow

1. Webhook arrives.
2. `webhook.py` turns it into either:
   - `full_review` for MR open/reopen/draft-to-ready
   - `discussion_reconcile` for MR note events
3. `store.py` enqueues the run in SQLite.
4. Newer full-review runs supersede older queued/running runs on the same MR.
5. Discussion reconcile runs collapse to the newest one unless a full review is already queued/running.
6. Full review:
   - `gitops.py` prepares checkout and diff
   - `prompts.py` builds a council review prompt with diff, metadata, repo rules, and discussion digest
   - `review_agent.py` runs Codex + Claude + synthesis
   - `service.py` maps findings to existing tracked findings and GitLab discussions
   - bot publishes inline discussions, human-thread replies, or summary-only fallback findings
   - `renderer.py` updates one summary note for the MR
7. Discussion reconcile:
   - loads the touched discussion
   - matches it to a tracked finding
   - runs a single-provider reconcile prompt
   - keeps the finding open, replies only, or marks it `dismissed_by_discussion`

## Important invariants

- Each MR has exactly one summary bot note.
- Findings can also have their own tracked GitLab discussions.
- Only one full-review SHA per MR is active at a time; newer full reviews supersede older queued/running ones.
- Push events to an already-open MR still do not auto-trigger a full review.
- Note events can trigger lightweight reconciliation when GitLab `note_events` are enabled.
- Human-owned threads are reply-only; the bot never resolves them.
- Bot-owned threads may be resolved automatically when a human explanation dismisses the concern.
- Findings that cannot be mapped to a valid diff position fall back to summary-only instead of failing the whole review.
- Inline publish failures degrade one finding to summary-only if the MR head is still current; if the MR head changed during publish, the run is treated as stale and superseded.
- `finding_match.py` deduplication is fuzzy; tuning thresholds affects false positives and false matches.
- Review traces are JSONL files in `REVIEW_TRACE_DIR`; inline thread detail lives in GitLab discussions, not in the summary note.

## Running tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python3 -m unittest discover -s tests
```
