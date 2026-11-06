"""把命令计划包装成队列任务，供批量转换与单文件操作共用。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ffmpeg_core.jobs import Job

from .converter import Plan


def job_from_plan(plan: Plan, *, name: str | None = None, kind: str = "convert") -> Job:
    label = name or Path(plan.output).name
    return Job(
        name=label,
        commands=plan.commands,
        output=plan.output,
        duration_s=plan.duration_s,
        kind=kind,
    )


def jobs_from_plans(plans: Iterable[tuple[str, Plan]], *, kind: str = "convert") -> list[Job]:
    return [job_from_plan(plan, name=name, kind=kind) for name, plan in plans]
