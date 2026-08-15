"""Bounded disk-retention policy for the unattended server runtime.

The module deletes only expired runtime artifacts and disposable cache files.
It protects active job inputs, states, and outcomes, and never reads the task
inbox or runs the business pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from typing import Callable, Iterable

from . import server_jobs


@dataclass(frozen=True)
class RetentionPaths:
    project_root: Path
    accepted_root: Path
    deliveries_root: Path
    jobs_root: Path
    outcomes_root: Path
    logs_root: Path
    cache_root: Path
    report_path: Path


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def remove_old_entries(
    root: Path,
    *,
    cutoff: datetime,
    protected: set[Path] | None = None,
) -> int:
    removed = 0
    protected_values = {path.resolve() for path in (protected or set())}
    if not root.exists():
        return 0
    entries = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for entry in entries:
        try:
            if entry.resolve() in protected_values:
                continue
            if entry.is_file():
                modified = datetime.fromtimestamp(
                    entry.stat().st_mtime,
                    tz=timezone.utc,
                )
                if modified < cutoff:
                    entry.unlink()
                    removed += 1
            elif entry.is_dir() and not any(entry.iterdir()):
                entry.rmdir()
        except OSError:
            continue
    return removed


def prune_cache_until(
    cache_root: Path,
    *,
    target_free_gb: float,
    disk_free: Callable[[], float],
    processor_active: Callable[[], bool],
) -> int:
    if not cache_root.exists() or processor_active():
        return 0
    files: list[tuple[float, Path]] = []
    for path in cache_root.rglob("*"):
        try:
            if path.is_file():
                files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    removed = 0
    for _modified, path in sorted(files):
        if disk_free() >= target_free_gb:
            break
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def run_retention(
    *,
    paths: RetentionPaths,
    states: Iterable[server_jobs.JobState],
    active_statuses: set[str],
    processor_active: Callable[[], bool],
    now: datetime | None = None,
    disk_free: Callable[[], float] | None = None,
    cache_retention_days: int = 2,
) -> dict:
    """Apply retention policy and atomically persist a small report."""
    now_value = now or datetime.now(timezone.utc)
    free_reader = disk_free or (lambda: disk_free_gb(paths.project_root))
    state_snapshot = list(states)
    active_sources = {
        Path(state.source_path)
        for state in state_snapshot
        if state.status in active_statuses and state.source_path
    }
    active_state_paths = {
        paths.jobs_root / f"{state.sha256}.json"
        for state in state_snapshot
        if state.status in active_statuses
    }
    active_outcomes = {
        Path(state.outcome_path)
        for state in state_snapshot
        if state.status in active_statuses and state.outcome_path
    }
    report: dict[str, int | float | str] = {
        "accepted_removed": remove_old_entries(
            paths.accepted_root,
            cutoff=now_value - timedelta(days=90),
            protected=active_sources,
        ),
        "deliveries_removed": remove_old_entries(
            paths.deliveries_root,
            cutoff=now_value - timedelta(days=90),
        ),
        "job_states_removed": remove_old_entries(
            paths.jobs_root,
            cutoff=now_value - timedelta(days=90),
            protected=active_state_paths,
        ),
        "outcomes_removed": remove_old_entries(
            paths.outcomes_root,
            cutoff=now_value - timedelta(days=90),
            protected=active_outcomes,
        ),
        "logs_removed": remove_old_entries(
            paths.logs_root,
            cutoff=now_value - timedelta(days=30),
        ),
    }
    cache_removed = 0
    if not processor_active():
        cache_removed = remove_old_entries(
            paths.cache_root,
            cutoff=now_value - timedelta(days=max(0, cache_retention_days)),
        )
    if free_reader() < 30.0:
        cache_removed += prune_cache_until(
            paths.cache_root,
            target_free_gb=50.0,
            disk_free=free_reader,
            processor_active=processor_active,
        )
    report["cache_removed"] = cache_removed
    report["free_disk_gb"] = round(free_reader(), 2)
    report["finished_at"] = server_jobs.utc_now()
    server_jobs.atomic_json(paths.report_path, report)
    return report


__all__ = [
    "RetentionPaths",
    "disk_free_gb",
    "prune_cache_until",
    "remove_old_entries",
    "run_retention",
]
