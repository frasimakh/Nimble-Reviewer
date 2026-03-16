from __future__ import annotations

import logging
import threading

from nimble_reviewer.service import ReviewService
from nimble_reviewer.store import Store

LOGGER = logging.getLogger(__name__)


class WorkerManager:
    def __init__(self, store: Store, service: ReviewService, concurrency: int, poll_interval_sec: float) -> None:
        self.store = store
        self.service = service
        self.concurrency = concurrency
        self.poll_interval_sec = poll_interval_sec
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        LOGGER.info(
            "Starting worker manager with concurrency=%s poll_interval_sec=%s",
            self.concurrency,
            self.poll_interval_sec,
        )
        for index in range(self.concurrency):
            thread = threading.Thread(target=self._run_loop, name=f"review-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
            LOGGER.info("Started worker thread name=%s", thread.name)

    def stop(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
            LOGGER.info("Stopped worker thread name=%s alive=%s", thread.name, thread.is_alive())

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            run = self.store.claim_next_run()
            if not run:
                self._stop_event.wait(self.poll_interval_sec)
                continue
            LOGGER.info(
                "Worker claimed run_id=%s project=%s mr=%s sha=%s",
                run.id,
                run.project_id,
                run.mr_iid,
                run.source_sha[:12],
            )
            try:
                self.service.process_run(run)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unhandled failure while processing run %s", run.id)
