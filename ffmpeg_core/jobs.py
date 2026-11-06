"""批量任务队列：QThreadPool + QRunnable，所有任务在后台线程执行。"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from config.logging_setup import get_logger
from config.settings import settings

from .ffmpeg_engine import run_commands

_log = get_logger("jobs")


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    name: str
    commands: list[list[str]]
    output: str = ""
    duration_s: float = 0.0
    status: JobStatus = JobStatus.PENDING
    progress: int = 0
    message: str = ""
    kind: str = "convert"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def status_key(self) -> str:
        return f"status.{self.status.value}"


class _Signals(QObject):
    changed = Signal(object)
    finished = Signal()


class _Runnable(QRunnable):
    def __init__(self, job: Job, signals: _Signals):
        super().__init__()
        self.job = job
        self.signals = signals

    def _emit_changed(self) -> None:
        try:
            self.signals.changed.emit(self.job)
        except RuntimeError:
            pass  # 窗口关闭后信号源可能已销毁

    def _emit_finished(self) -> None:
        try:
            self.signals.finished.emit()
        except RuntimeError:
            pass

    def _progress(self, percent: int, text: str) -> None:
        self.job.progress = max(0, min(100, percent))
        self.job.message = text
        self._emit_changed()

    def run(self) -> None:
        job = self.job
        job.status = JobStatus.RUNNING
        job.progress = 0
        job.message = ""
        self._emit_changed()
        try:
            result = run_commands(
                job.commands,
                on_progress=self._progress,
                cancel_event=job.cancel_event,
                duration_s=job.duration_s,
            )
            if job.cancel_event.is_set():
                job.status = JobStatus.CANCELLED
                job.message = "已取消"
            elif result.returncode != 0:
                job.status = JobStatus.FAILED
                job.message = (result.stderr.strip() or f"ffmpeg 退出码 {result.returncode}")[-300:]
            else:
                job.status = JobStatus.DONE
                job.progress = 100
                job.message = "完成"
        except Exception as exc:  # 后台线程不碰 UI，只记日志
            job.status = JobStatus.FAILED
            job.message = str(exc)[:300]
            _log.exception("任务异常 %s: %s", job.name, exc)
        self._emit_changed()
        self._emit_finished()


class JobQueue(QObject):
    job_changed = Signal(object)
    queue_finished = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(settings().get("concurrency", 2))))
        self._jobs: list[Job] = []
        self._runnables: list[_Runnable] = []
        self._signals = _Signals()
        self._signals.changed.connect(self.job_changed)
        self._signals.finished.connect(self._one_finished)
        self._active = 0
        self._lock = threading.Lock()

    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def add(self, job: Job) -> Job:
        self._jobs.append(job)
        self.job_changed.emit(job)
        _log.info("加入队列: %s (%d 条命令)", job.name, len(job.commands))
        return job

    def set_concurrency(self, count: int) -> None:
        count = max(1, min(8, int(count)))
        self._pool.setMaxThreadCount(count)
        settings().set("concurrency", count)
        _log.info("并发数设为 %d", count)

    def start(self) -> int:
        pending = [j for j in self._jobs if j.status == JobStatus.PENDING]
        for job in pending:
            runnable = _Runnable(job, self._signals)
            self._runnables.append(runnable)
            with self._lock:
                self._active += 1
            self._pool.start(runnable)
        return len(pending)

    def cancel(self, job_id: str) -> None:
        for job in self._jobs:
            if job.id == job_id and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                job.cancel_event.set()
                if job.status == JobStatus.PENDING:
                    job.status = JobStatus.CANCELLED
                    job.message = "已取消"
                    self.job_changed.emit(job)

    def cancel_all(self) -> None:
        for job in self._jobs:
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                self.cancel(job.id)

    def remove(self, job_id: str) -> None:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return
        if job.status == JobStatus.RUNNING:
            job.cancel_event.set()
        self._jobs = [j for j in self._jobs if j.id != job_id]
        self._runnables = [r for r in self._runnables if r.job.id != job_id]

    def clear_finished(self) -> None:
        finished = {j.id for j in self._jobs if j.status in
                    (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)}
        self._jobs = [j for j in self._jobs if j.id not in finished]
        self._runnables = [r for r in self._runnables if r.job.id not in finished]

    def clear_all(self) -> None:
        self.cancel_all()
        self._jobs.clear()
        self._runnables.clear()

    def _one_finished(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            empty = self._active == 0
        if empty:
            self.queue_finished.emit()
