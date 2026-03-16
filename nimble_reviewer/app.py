from __future__ import annotations

import logging
from json import JSONDecodeError
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response

from nimble_reviewer.config import Settings
from nimble_reviewer.gitlab import GitLabClient
from nimble_reviewer.gitops import RepoManager
from nimble_reviewer.review_agent import ClaudeRunner, CodexRunner, CouncilRunner
from nimble_reviewer.runtime_state import prepare_claude_state
from nimble_reviewer.service import ReviewService, ServiceDependencies
from nimble_reviewer.store import Store
from nimble_reviewer.trace import TraceSettings
from nimble_reviewer.webhook import parse_merge_request_event
from nimble_reviewer.worker import WorkerManager

LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()
    _configure_logging()
    LOGGER.info(
        "Starting nimble-reviewer with council=codex+claude synthesis_provider=%s sqlite=%s cache_dir=%s trace_dir=%s concurrency=%s timeout_sec=%s",
        cfg.council_synthesis_provider,
        cfg.sqlite_path,
        cfg.repo_cache_dir,
        cfg.review_trace_dir,
        cfg.max_concurrent_reviews,
        cfg.review_timeout_sec,
    )

    store = Store(cfg.sqlite_path)
    store.initialize()
    cfg.repo_cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_trace_dir.mkdir(parents=True, exist_ok=True)
    prepare_claude_state(Path.home())

    service = ReviewService(
        ServiceDependencies(
            store=store,
            gitlab=GitLabClient(cfg.gitlab_url, cfg.gitlab_token),
            repo_manager=RepoManager(cfg.repo_cache_dir, cfg.gitlab_git_username, cfg.gitlab_token),
            review_agent=_build_review_agent(cfg),
            trace_settings=TraceSettings(cfg.review_trace_dir),
        )
    )
    workers = WorkerManager(store, service, cfg.max_concurrent_reviews, cfg.poll_interval_sec)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        workers.start()
        try:
            yield
        finally:
            workers.stop()

    app = FastAPI(title="nimble-reviewer", lifespan=lifespan)
    app.state.settings = cfg
    app.state.store = store

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/gitlab", status_code=202)
    async def gitlab_webhook(
        request: Request,
        response: Response,
        x_gitlab_token: str | None = Header(default=None),
    ) -> dict[str, str | int]:
        if x_gitlab_token != cfg.gitlab_webhook_secret:
            LOGGER.warning("Rejected webhook request due to invalid X-Gitlab-Token")
            raise HTTPException(status_code=401, detail="Invalid webhook token")

        try:
            payload = await request.json()
        except (JSONDecodeError, ValueError) as exc:
            LOGGER.warning("Rejected webhook request due to invalid JSON payload: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        object_kind = payload.get("object_kind")
        attributes = payload.get("object_attributes") or {}
        action = attributes.get("action") or "unknown"
        state = attributes.get("state") or "unknown"
        project_id = (payload.get("project") or {}).get("id", "unknown")
        mr_iid = attributes.get("iid", "unknown")

        event = parse_merge_request_event(payload)
        if not event:
            LOGGER.info(
                "Ignored webhook project=%s mr=%s object_kind=%s action=%s state=%s draft=%s sha=%s",
                project_id,
                mr_iid,
                object_kind,
                action,
                state,
                bool(attributes.get("work_in_progress") or attributes.get("draft")),
                _short_sha(
                    (attributes.get("last_commit") or {}).get("id")
                    or attributes.get("last_commit_id")
                    or attributes.get("sha")
                ),
            )
            response.status_code = 202
            return {"status": "ignored"}

        decision = store.enqueue_run(event.project_id, event.mr_iid, event.source_sha, event.target_sha)
        LOGGER.info(
            "Webhook decision status=%s reason=%s run_id=%s project=%s mr=%s action=%s sha=%s target_sha=%s",
            "queued" if decision.enqueued else "ignored",
            decision.reason,
            decision.run_id,
            event.project_id,
            event.mr_iid,
            event.action,
            _short_sha(event.source_sha),
            _short_sha(event.target_sha),
        )
        return {
            "status": "queued" if decision.enqueued else "ignored",
            "reason": decision.reason,
            "run_id": decision.run_id or 0,
        }

    return app


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_review_agent(settings: Settings):
    codex_runner = CodexRunner(settings.codex_cmd, settings.review_timeout_sec)
    claude_runner = ClaudeRunner(settings.claude_cmd, settings.review_timeout_sec)
    synthesis_runner = (
        CodexRunner(settings.council_synthesis_cmd, settings.review_timeout_sec)
        if settings.council_synthesis_provider == "codex"
        else ClaudeRunner(settings.council_synthesis_cmd, settings.review_timeout_sec)
    )
    return CouncilRunner(
        codex_runner=codex_runner,
        claude_runner=claude_runner,
        synthesizer=synthesis_runner,
    )


def _short_sha(value: str | None) -> str:
    if not value:
        return "-"
    return value[:12]
