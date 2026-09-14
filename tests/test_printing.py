"""Tests for job archiving. Writes to tmp_path only, per the core testing rule.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

import json
from pathlib import Path

from py_printer_server.printing import Job, JobFile, JobQueue, PrintOptions


def _make_job(files: list[str]) -> Job:
    return Job(
        id="test123",
        files=[JobFile(name=n, status="done") for n in files],
        options=PrintOptions(printer="Test Printer", color=True, paper="A4", copies=1, duplex=False),
    )


def test_archive_moves_files_and_writes_job_json(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    jobs = tmp_path / "jobs"
    spool.mkdir()
    jobs.mkdir()
    (spool / "a.txt").write_text("hello", encoding="utf-8")
    (spool / "b.txt").write_text("world", encoding="utf-8")

    queue = JobQueue(jobs, dry_run=True)
    job = _make_job(["a.txt", "b.txt"])
    job.status = "done"
    import time
    job.finished = time.time()

    queue._archive(job, spool)

    assert not (spool / "a.txt").exists()
    assert not (spool / "b.txt").exists()

    archives = list(jobs.iterdir())
    assert len(archives) == 1
    archive_dir = archives[0]
    assert archive_dir.name.startswith("print-job-")
    assert (archive_dir / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (archive_dir / "b.txt").read_text(encoding="utf-8") == "world"

    record = json.loads((archive_dir / "job.json").read_text(encoding="utf-8"))
    assert record["id"] == "test123"
    assert record["status"] == "done"
    assert len(record["files"]) == 2


def test_archive_does_not_overwrite_name_collision(tmp_path: Path) -> None:
    """Two jobs archiving a same-named file get their own job-id-suffixed
    folders (not colliding filenames within one folder), since each job's
    archive directory embeds its own id."""
    spool = tmp_path / "spool"
    jobs = tmp_path / "jobs"
    spool.mkdir()
    jobs.mkdir()
    (spool / "a.txt").write_text("first", encoding="utf-8")

    queue = JobQueue(jobs, dry_run=True)
    job1 = _make_job(["a.txt"])
    job1.created = 1000.0
    queue._archive(job1, spool)

    (spool / "a.txt").write_text("second", encoding="utf-8")
    job2 = _make_job(["a.txt"])
    job2.id = "test456"
    job2.created = 1000.0
    queue._archive(job2, spool)

    archive_dirs = {d.name: d for d in jobs.iterdir()}
    assert len(archive_dirs) == 2

    dir1 = next(d for name, d in archive_dirs.items() if name.endswith("test123"))
    dir2 = next(d for name, d in archive_dirs.items() if name.endswith("test456"))

    assert (dir1 / "a.txt").read_text(encoding="utf-8") == "first"
    assert (dir2 / "a.txt").read_text(encoding="utf-8") == "second"


def test_archive_skips_missing_source_file(tmp_path: Path) -> None:
    """A file that was deleted from the spool between submission and
    archiving (e.g. by a concurrent /delete) should not crash the worker."""
    spool = tmp_path / "spool"
    jobs = tmp_path / "jobs"
    spool.mkdir()
    jobs.mkdir()

    queue = JobQueue(jobs, dry_run=True)
    job = _make_job(["never_existed.txt"])
    queue._archive(job, spool)  # must not raise

    archives = list(jobs.iterdir())
    assert len(archives) == 1
    assert (archives[0] / "job.json").exists()
