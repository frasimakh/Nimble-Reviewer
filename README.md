# Nimble Reviewer

Containerized GitLab merge request review bot. The service accepts GitLab merge request webhooks, queues persisted review runs in SQLite, checks out the MR, runs Codex and Claude Code reviews in parallel, then lets a final synthesis model produce one bot comment back into the merge request.

Review runs are triggered when a non-draft MR is opened or reopened, and when an existing MR moves from draft to ready. Pushing new commits to an already open MR does not trigger an automatic re-review.

## Repository-specific review rules

The reviewer can load repo-local prompt instructions directly from the checked-out repository. On each run it looks for a single file in the repo root:

- `NIMBLE-REVIEWER.MD`

If that file exists and is non-empty, it is appended to the review prompt, so each repository can define its own review policy without changing the service image.

Typical uses:

- define what counts as critical or release-blocking
- tell the reviewer which folders deserve extra scrutiny
- describe architectural invariants that must not be broken
- list categories that should usually be ignored

Example `NIMBLE-REVIEWER.MD`:

```md
# Review rules

- Treat data-loss, auth, and concurrency bugs as high severity.
- Pay extra attention to `src/payments/` and `db/migrations/`.
- Ignore formatting-only comments unless they hide a real defect.
- Flag changes that bypass tenant isolation checks.
```

Repository rules are capped before being injected into the prompt so an oversized rules file does not crowd out the diff.

## Required environment

- `GITLAB_URL` should be the base URL of your self-hosted GitLab instance, for example `https://gitlab.example.com`
- `GITLAB_URL`
- `GITLAB_TOKEN`
- `GITLAB_WEBHOOK_SECRET`
- `CODEX_CMD`
- `CLAUDE_CMD`
- `SQLITE_PATH`
- `REPO_CACHE_DIR`
- `REVIEW_TIMEOUT_SEC`
- `MAX_CONCURRENT_REVIEWS`

Optional:

- `COUNCIL_SYNTHESIS_PROVIDER` defaults to `codex`
- `COUNCIL_SYNTHESIS_CMD` defaults to Codex `gpt-5.4` with `low` reasoning, or Claude `sonnet` with `low` reasoning if the synthesis provider is set to `claude`
- `REVIEW_TRACE_DIR` defaults to `/data/review-traces`
- `GITLAB_GIT_USERNAME` defaults to `oauth2`
- `PORT` defaults to `8080` and controls the HTTP port inside the container
- `HOST_PORT` defaults to `8080` and controls the published port on the host in `docker compose`
- any auth env required by the configured CLI command if you choose API-key auth

## Review council configuration

The service always runs the same three-step council flow:

1. Codex base review
2. Claude base review
3. Final synthesis into one MR note using both base reviews

Default command behavior:

```env
CODEX_CMD=codex exec -m gpt-5.4 -c model_reasoning_effort="high" -
CLAUDE_CMD=claude -p --output-format stream-json --model sonnet --effort high --permission-mode bypassPermissions
COUNCIL_SYNTHESIS_PROVIDER=codex
COUNCIL_SYNTHESIS_CMD=codex exec -m gpt-5.4 -c model_reasoning_effort="low" -
```

For Codex, the command should read the prompt from `stdin` and print the final review JSON to `stdout`.

For Claude Code, the service supports:

- plain text mode where Claude prints the final review JSON directly
- `--output-format json`, where Claude wraps the final review text in a JSON envelope and the service extracts the `result` field
- `--output-format stream-json`, where the service records Claude provider events into the persisted run trace and extracts the final `result` event

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

The MR note stays intentionally short. It includes:

- council participants
- model and reasoning effort for each participant
- token usage when available
- Claude `cost_usd` when available
- per-finding model attribution: `codex`, `claude`, or `both`

It does not include a separate `Review Trace` section.

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

OpenAI documents both ChatGPT sign-in for the CLI and ChatGPT-managed auth for automation. Treat the persisted auth state as a secret.

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
