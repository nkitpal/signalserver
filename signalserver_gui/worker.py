"""PostgreSQL-backed job queue.

Jobs are claimed with ``SELECT ... FOR UPDATE SKIP LOCKED`` so any number of
worker processes can run side by side without Redis or another broker.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import session_factory
from .elevation.provider import ElevationProvider
from .elevation.tiles import tiles_for_bbox
from .engine.runner import execute_run
from .models import Job, JobStatus, PlotRun

log = logging.getLogger(__name__)

MAX_PREFETCH_TILES = 400


def now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(session: Session, kind: str, payload: dict) -> Job:
    job = Job(kind=kind, payload=payload, status=JobStatus.queued, stage="queued")
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session) -> Job | None:
    job = session.scalar(
        select(Job)
        .where(Job.status == JobStatus.queued)
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = JobStatus.running
    job.started_at = job.heartbeat_at = now()
    job.attempts += 1
    job.stage = "starting"
    session.commit()
    return job


def requeue_stale(session: Session, stale_after_s: int) -> int:
    cutoff = now() - timedelta(seconds=stale_after_s)
    res = session.execute(
        update(Job)
        .where(Job.status == JobStatus.running, Job.heartbeat_at < cutoff, Job.attempts < 3)
        .values(status=JobStatus.queued, stage="requeued")
    )
    session.commit()
    return res.rowcount or 0


class Worker:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.Session = session_factory(self.settings.database_url)
        self._stop = threading.Event()

    def stop(self, *_):
        self._stop.set()

    def run_forever(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        log.info("Worker started")
        last_sweep = 0.0
        while not self._stop.is_set():
            if time.monotonic() - last_sweep > 60:
                with self.Session() as s:
                    if n := requeue_stale(s, self.settings.worker_stale_after_s):
                        log.warning("Requeued %d stale job(s)", n)
                last_sweep = time.monotonic()
            if not self.run_once():
                self._stop.wait(self.settings.worker_poll_interval_s)
        log.info("Worker stopped")

    def run_once(self) -> bool:
        with self.Session() as session:
            job = claim_next(session)
            if job is None:
                return False
            log.info("Running job %s (%s)", job.id, job.kind)
            job_id = job.id

            def progress(frac: float, stage: str) -> None:
                # Separate session: progress must be visible before the job commits.
                with self.Session() as ps:
                    ps.execute(
                        update(Job).where(Job.id == job_id).values(
                            progress=round(frac, 3), stage=stage[:80], heartbeat_at=now()
                        )
                    )
                    ps.commit()

            try:
                self._dispatch(session, job, progress)
                job = session.get(Job, job_id)
                job.status = JobStatus.done
                job.progress = 1.0
                job.stage = "done"
                job.finished_at = now()
                session.commit()
            except Exception as exc:  # noqa: BLE001 - record any failure on the job
                log.exception("Job %s failed", job_id)
                session.rollback()
                job = session.get(Job, job_id)
                job.status = JobStatus.failed
                job.error = str(exc)[-8000:]
                job.stage = "failed"
                job.finished_at = now()
                session.commit()
            return True

    def _dispatch(self, session: Session, job: Job, progress) -> None:
        if job.kind == "plot_run":
            run = session.get(PlotRun, job.payload["run_id"])
            if run is None:
                raise RuntimeError("plot run no longer exists")
            execute_run(run, session, self.settings, progress)
            session.commit()
        elif job.kind == "elevation_prefetch":
            bbox = job.payload["bbox"]
            tiles = tiles_for_bbox(*bbox)
            if len(tiles) > MAX_PREFETCH_TILES:
                raise ValueError(f"area too large: {len(tiles)} tiles (max {MAX_PREFETCH_TILES})")
            ElevationProvider(self.settings, session).ensure_tiles(tiles, progress)
        else:
            raise ValueError(f"unknown job kind {job.kind!r}")


def start_background_worker(settings: Settings | None = None) -> Worker:
    worker = Worker(settings)
    threading.Thread(target=worker.run_forever, name="ssg-worker", daemon=True).start()
    return worker
