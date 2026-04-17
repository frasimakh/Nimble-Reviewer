# Nimble Reviewer

Containerized GitLab merge request review bot. The service accepts GitLab merge request and note webhooks, queues persisted review runs in SQLite, checks out the MR, runs Codex and Claude Code reviews in parallel, then publishes:

- GitLab discussion threads for findings, inline when possible and top-level otherwise
- lightweight discussion reconciliation when humans reply in MR threads

Full review runs are triggered when a non-draft MR is opened or reopened, when an existing MR moves from draft to ready, and when new commits are pushed to an already-open ready MR. Human comments in merge request discussions can trigger a lightweight `discussion_reconcile` run when GitLab `note_events` are enabled.

## Repository-specific review rules

The reviewer can load repo-local prompt instructions directly from the checked-out repository. On each full review run it looks for a single file in the repo root:

- `NIMBLE-REVIEWER.MD`

If that file exists and is non-empty, it is appended to the review prompt, so each repository can define its own review policy without changing the service image.

Typical uses:

- define what counts as critical or release-blocking
- tell the reviewer which folders deserve extra scrutiny
- describe architectural invariants that must not be broken
- list categories that should usually be ignored
- keep repository-specific review policy current as the codebase evolves

Example `NIMBLE-REVIEWER.MD`:

```md
# Review rules

- Treat data-loss, auth, and concurrency bugs as high severity.
- Pay extra attention to `src/payments/` and `db/migrations/`.
- Ignore formatting-only comments unless they hide a real defect.
- Flag changes that bypass tenant isolation checks.
```

Repository rules are capped before being injected into the prompt so an oversized rules file does not crowd out the diff.

## Review flows

The service now has two review flows.

### Full review

The full review flow always runs the council:

1. Codex base review
2. Claude base review
3. Final synthesis into one merged review result

The synthesized findings are then published as:

- inline discussions when the finding maps to a changed diff line
- top-level merge request discussions when the finding cannot be anchored safely
- replies in relevant human threads when the concern matches an existing discussion

There is no persistent note for completed reviews — findings live in their own discussion threads.

### Discussion reconcile

When a human adds or edits a merge request note and `note_events` are enabled in GitLab, the service can enqueue a lightweight `discussion_reconcile` run instead of a full council review.

This reconcile flow:

- loads the touched discussion
- matches it to an existing tracked finding when possible
- runs a single-provider decision pass
- may keep the finding open, reply only, or mark it `dismissed_by_discussion`

Bot-owned threads may be auto-resolved when a human explanation convincingly dismisses the concern. Human-owned threads are reply-only; the bot never resolves them.

## Data model

The service stores three kinds of state in SQLite.

**`merge_request_state`** — one row per MR:

| column | description |
|---|---|
| `project_id`, `mr_iid` | primary key |
| `last_seen_sha` | latest commit the service has observed |
| `last_reviewed_sha` | last commit where a full review completed successfully |

**`review_run`** — the work queue:

| column | description |
|---|---|
| `kind` | `full_review` or `discussion_reconcile` |
| `source_sha`, `target_sha` | commits being reviewed |
| `status` | queued → running → done / failed / superseded |
| `trigger_discussion_id`, `trigger_note_id`, `trigger_author_id` | set on reconcile runs to identify what triggered them |

**`tracked_finding`** — every finding across all reviews:

| column | description |
|---|---|
| `fingerprint` | deterministic hash of (file, line, severity, title, body); the stable identity of a finding across reviews |
| `status` | `open`, `resolved`, or `dismissed_by_discussion` |
| `discussion_id`, `root_note_id` | links to the GitLab discussion thread, if one was created |
| `thread_owner` | `bot`, `human`, or `summary-only` |
| `opened_sha`, `last_seen_sha`, `resolved_sha`, `dismissed_sha` | commit at which each status transition happened |

## Full review — detailed flow

### 1. Webhook → queue

A GitLab merge request event is parsed and turned into a `full_review` run when:

- a non-draft MR is opened or reopened
- the MR title changes from draft to ready
- new commits are pushed to an already-open ready MR

The extracted data is:

```
project_id, mr_iid       — which MR
source_sha, target_sha   — current commits
```

Enqueue rules:
- If a `full_review` for the same project/MR/SHA already exists in queued, running, or done state, the new request is dropped as a duplicate.
- When a new `full_review` is enqueued, any previous queued or running run for the same MR is marked superseded.

### 2. Repository checkout and diff

`gitops.py` prepares the working directory:

1. Maintains a bare clone mirror per repository. On first use it runs `git clone --mirror`; on subsequent runs it runs `git fetch --prune`.
2. Creates a temporary checkout from the mirror and checks out `source_sha`.
3. Computes the merge base between the MR head and the target branch.
4. Builds two diffs:
   - **full diff** — `git diff <merge_base>..<source_sha>` — the complete set of changes in the MR
   - **incremental diff** — `git diff <previous_reviewed_sha>..<source_sha>` — only what changed since the last completed review; empty on the first review of an MR, and skipped when the target branch SHA changed since the previous full review so rebases do not inject `develop` noise into the review focus
5. Loads `NIMBLE-REVIEWER.MD` from the repo root if present.

### 3. Review prompt

`prompts.py` assembles the prompt sent to the council:

- MR metadata: title, source and target branches, SHA, URL, description
- Repository rules from `NIMBLE-REVIEWER.MD` (capped to avoid crowding out the diff)
- Open discussion digest: a summary of unresolved threads on changed files with all their notes, so the council knows what is already under discussion
- List of changed files
- Incremental diff, labelled "focus primarily on these changes", so the council avoids re-reporting concerns from unchanged code; omitted when the MR was rebased onto a newer target branch SHA because that diff would mostly show base-branch churn instead of author changes
- Surrounding file context: lines around each changed hunk read from the checkout, so the council sees the full function/class context beyond the diff
- Full unified diff (capped at 200 k characters)

The council returns JSON:

```json
{
  "summary": "short review summary",
  "overall_risk": "high | medium | low",
  "findings": [
    {
      "severity": "high | medium | low",
      "file": "path/to/file.py",
      "line": 123,
      "title": "short title",
      "body": "actionable explanation",
      "suggestion": "optional fix direction"
    }
  ]
}
```

### 4. Publishing findings

For each finding in the council result:

1. **Fingerprint lookup** — if a `tracked_finding` with the same fingerprint exists, its discussion thread is reused.
2. **Fuzzy match** — if no exact fingerprint match, the service looks for an existing finding with matching file, line, and content.
3. **New thread** — if no match exists, the service tries to post an inline diff discussion anchored to the exact code line. If that fails (position not in diff, invalid mapping), it falls back to a top-level MR discussion. If the MR head moved during publish, the finding falls back to summary-only and the run is superseded.

Findings that appeared in the previous review and are still present get a "still present at `<sha>`" reply added to their thread. The reply includes a hidden marker so it is never duplicated across runs.

Findings from the previous review that are no longer present are resolved: their `tracked_finding` status is set to `resolved` and the GitLab discussion is marked resolved.

## Discussion reconcile — detailed flow

### 1. Trigger

When a human adds a note to a merge request discussion and GitLab `note_events` are enabled, the service enqueues a `discussion_reconcile` run. The note must be on a merge request (not a commit or snippet), and bot-authored notes are ignored.

A reconcile run is not enqueued if a `full_review` is already queued or running for that MR — the full review will pick up the latest discussion state when it runs.

### 2. Prompt

`prompts.py` builds the reconcile prompt with:

- MR metadata
- The original finding: fingerprint, severity, file, line, title, body
- A diff excerpt for the file and line mentioned in the finding
- The full discussion thread: all notes in order
- The latest human note that triggered the run

### 3. Decision

A single provider (configurable via `DISCUSSION_RECONCILE_PROVIDER`) returns one of four decisions:

| decision | meaning | action |
|---|---|---|
| `dismissed_by_discussion` | human provided a concrete explanation or explicit acceptance | set finding to `dismissed_by_discussion`, post a reply, resolve the thread if bot-owned |
| `reply_only` | finding stays open but a reply is useful | post a reply, keep finding open |
| `keep_open` | human did not engage substantively | post a reply re-explaining the concern, keep finding open |
| `no_action` | off-topic or noise | do nothing |

Human-owned threads are always reply-only; the bot never resolves them regardless of the decision.

### 4. Suppression on next full review

A finding marked `dismissed_by_discussion` is not re-reported in subsequent full reviews unless the diff has new changes near the dismissed line. This prevents the bot from re-raising concerns that a human has already accepted.

## Required environment

- `GITLAB_URL` should be the base URL of your self-hosted GitLab instance, for example `https://gitlab.example.com`
- `GITLAB_TOKEN`
- `GITLAB_WEBHOOK_SECRET`
- `CODEX_CMD`
- `CLAUDE_CMD`
- `SQLITE_PATH`
- `REPO_CACHE_DIR`

Optional:

- `COUNCIL_SYNTHESIS_PROVIDER` defaults to `codex`
- `DISCUSSION_RECONCILE_PROVIDER` defaults to the same provider as `COUNCIL_SYNTHESIS_PROVIDER`
- `REVIEW_TRACE_DIR` defaults to `/data/review-traces`
- `REVIEW_TIMEOUT_SEC` defaults to `600`
- `MAX_CONCURRENT_REVIEWS` defaults to `1`
- `POLL_INTERVAL_SEC` defaults to `1.0`
- `GITLAB_GIT_USERNAME` defaults to `oauth2`
- `PORT` defaults to `8080` and controls the HTTP port inside the container
- `HOST_PORT` defaults to `8080` and controls the published port on the host in `docker compose`
- any auth env required by the configured CLI command if you choose API-key auth

## Review council configuration

Default command behavior:

```env
CODEX_CMD=codex exec -m gpt-5.4 -c model_reasoning_effort="high" -
CLAUDE_CMD=claude -p --output-format stream-json --model sonnet --effort high --permission-mode bypassPermissions
COUNCIL_SYNTHESIS_PROVIDER=codex
DISCUSSION_RECONCILE_PROVIDER=codex
```

For Codex, the command should read the prompt from `stdin` and print the final review JSON to `stdout`.

For Claude Code, the service supports:

- plain text mode where Claude prints the final review JSON directly
- `--output-format json`, where Claude wraps the final review text in a JSON envelope and the service extracts the `result` field
- `--output-format stream-json`, where the service records Claude provider events into the persisted run trace and extracts the final `result` event

## GitLab webhook setup

The service expects a webhook pointing to:

```text
http://<host-or-ip>:<port>/webhooks/gitlab
```

Enable at least:

- Merge request events

Enable this as well if you want live discussion reconciliation:

- Note events

The note webhook path is used only for merge request discussions. Non-MR notes, system notes, and bot-authored notes are ignored.

## Review trace

The service always writes a per-run trace file to disk:

```env
REVIEW_TRACE_DIR=/data/review-traces
```

Each run is written as `run-<id>.jsonl`.

For Codex runs, the trace includes:

- app events
- git checkout events
- Codex JSONL provider events
- token usage from the Codex event stream when available

For Claude runs, the trace includes:

- app events
- git checkout events
- Claude provider events when `CLAUDE_CMD` uses `--output-format stream-json` or `--output-format json`
- Claude token usage and `total_cost_usd` when the CLI includes usage fields in those events

Discussion details live in GitLab discussions, not in the trace. Failure notes are only used for failed runs and are cleaned up on the next successful full review.

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn nimble_reviewer.app:create_app --factory --host 0.0.0.0 --port 8080
```

## Docker run

```bash
cp .env.example .env
docker compose up --build
```

The default compose file persists SQLite data in `reviewer-data` and repository mirrors in `reviewer-cache`.

If port `8080` is already occupied on the host, leave `PORT=8080` and change only `HOST_PORT`, for example:

```env
PORT=8080
HOST_PORT=18080
```

Your GitLab webhook URL then becomes `http://<host-or-ip>:18080/webhooks/gitlab`.

### Manual `docker run`

On older Docker hosts without a working `docker compose` setup, you can run the container directly:

```bash
docker run -d \
  --name nimble-reviewer \
  --restart unless-stopped \
  -p 18080:8080 \
  --env-file .env \
  -v nimble-reviewer-data:/data \
  -v nimble-reviewer-cache:/cache \
  -v nimble-reviewer-auth:/home/reviewer/.codex \
  -v nimble-reviewer-claude-auth:/home/reviewer/.claude \
  nimble-reviewer:latest
```

This publishes the service on host port `18080`, so the webhook URL is `http://<host-or-ip>:18080/webhooks/gitlab`.

## Authentication modes

The service authenticates through both configured CLIs.

### Codex

- ChatGPT subscription auth: recommended if you want to use your Codex subscription instead of API billing.
- API key auth: optional if you want headless usage-based access.

### ChatGPT subscription auth in Docker

The compose file persists `/home/reviewer/.codex` in a dedicated volume so the login session survives container recreation.

1. Start the service:

```bash
docker compose up -d --build
```

2. Authenticate Codex inside the running container:

```bash
docker compose exec nimble-reviewer codex login --device-auth
```

3. Open the shown URL in a browser, enter the device code, and sign in with the ChatGPT account that has Codex access.

4. Verify login status:

```bash
docker compose exec nimble-reviewer codex login status
```

Treat the persisted auth state as a secret.

### If `codex login` fails with `Permission denied`

If you created the auth volume before the container knew about `/home/reviewer/.codex`, the volume may be owned by `root`. Fix it once:

```bash
docker exec -u 0 nimble-reviewer sh -lc 'mkdir -p /home/reviewer/.codex && chown -R reviewer:reviewer /home/reviewer/.codex /home/reviewer'
docker exec -e HOME=/home/reviewer -it nimble-reviewer codex login --device-auth
```

If that container was built from an older image, rebuild it before retrying:

```bash
docker build -t nimble-reviewer:latest .
```

### Claude Code

The compose file also persists `/home/reviewer/.claude` so Claude Code login and settings survive container recreation.

The council always uses Claude Code. You can customize the command if needed, for example:

```env
CLAUDE_CMD=claude -p --output-format stream-json --model sonnet --effort high --permission-mode bypassPermissions
```

Then start the service and authenticate Claude inside the container:

```bash
docker compose exec -it nimble-reviewer claude
```

Then run `/login` inside the Claude session and complete the browser-based sign-in flow.
