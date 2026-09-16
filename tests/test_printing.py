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


class TestWaitForJobId:
    """A job reported done must mean paper actually came out (see
    JobQueue._overall_status): print_text() only confirms the data reached
    the spooler, so _process_one must wait for the job to drain from the
    queue before marking a text file "done". Unlike the shell-verb wait,
    there is no "did it ever appear?" ambiguity for a job id StartDocPrinterW
    itself returned -- its absence at any point means finished.
    """

    def _queue(self, tmp_path: Path, sequence: list[set[int]]) -> JobQueue:
        jobs = tmp_path / "jobs"
        jobs.mkdir()
        q = JobQueue(jobs, dry_run=True)
        calls = iter(sequence)
        q._enum_job_ids = lambda printer: next(calls, sequence[-1])  # type: ignore[method-assign]
        return q

    def test_returns_true_immediately_if_already_gone(self, tmp_path: Path) -> None:
        q = self._queue(tmp_path, [set()])
        assert q._wait_for_job_id("Test Printer", 42, timeout=0.05) is True

    def test_returns_true_once_job_drains(self, tmp_path: Path, monkeypatch) -> None:
        from py_printer_server import printing
        monkeypatch.setattr(printing, "JOB_POLL_INTERVAL", 0.01)
        q = self._queue(tmp_path, [{42}, {42}, set()])
        assert q._wait_for_job_id("Test Printer", 42, timeout=1.0) is True

    def test_returns_false_if_still_present_at_deadline(self, tmp_path: Path, monkeypatch) -> None:
        from py_printer_server import printing
        monkeypatch.setattr(printing, "JOB_POLL_INTERVAL", 0.01)
        q = self._queue(tmp_path, [{42}])
        assert q._wait_for_job_id("Test Printer", 42, timeout=0.03) is False


class TestWaitForShellJob:
    """The shell handler never reports its job id back, so the job is found by
    diffing the queue -- which has to be done on ids, and has to tolerate a
    job that finishes quickly, or a file that did print gets reported as an
    error and left in the spool, where retrying it prints it twice.
    """

    def _queue(self, tmp_path: Path, sequence: list[set[int]]) -> JobQueue:
        jobs = tmp_path / "jobs"
        jobs.mkdir()
        q = JobQueue(jobs, dry_run=True)
        calls = iter(sequence)
        q._enum_job_ids = lambda printer: next(calls, sequence[-1])  # type: ignore[method-assign]
        return q

    def test_waits_for_job_to_appear_then_drain(self, tmp_path: Path, monkeypatch) -> None:
        from py_printer_server import printing
        monkeypatch.setattr(printing, "JOB_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_TIMEOUT", 1.0)
        q = self._queue(tmp_path, [set(), {7}, {7}, set()])
        assert q._wait_for_shell_job("Test Printer", "a.pdf", set()) is True

    def test_reports_failure_when_no_job_ever_appears(self, tmp_path: Path, monkeypatch) -> None:
        """A handler stuck on a first-run dialog spools nothing; that must not
        be reported as printed."""
        from py_printer_server import printing
        monkeypatch.setattr(printing, "JOB_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_TIMEOUT", 0.05)
        q = self._queue(tmp_path, [set()])
        assert q._wait_for_shell_job("Test Printer", "a.pdf", set()) is False

    def test_duplicate_document_name_does_not_hide_the_new_job(self, tmp_path: Path, monkeypatch) -> None:
        """Printing the same file twice puts two identically named jobs on the
        queue. Diffing on ids still spots the second one; diffing on names --
        which this used to do -- saw no change and timed out as a failure."""
        from py_printer_server import printing
        monkeypatch.setattr(printing, "JOB_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_TIMEOUT", 1.0)
        # Job 1 is already queued under the same document name; ours is id 2.
        q = self._queue(tmp_path, [{1}, {1, 2}, {1}])
        assert q._wait_for_shell_job("Test Printer", "a.pdf", {1}) is True

    def test_does_not_wait_on_another_applications_job(self, tmp_path: Path, monkeypatch) -> None:
        """Ours drained; an unrelated job queued meanwhile is not ours to wait
        for."""
        from py_printer_server import printing
        monkeypatch.setattr(printing, "JOB_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(printing, "JOB_APPEAR_TIMEOUT", 1.0)
        q = self._queue(tmp_path, [{5}, {9}])
        assert q._wait_for_shell_job("Test Printer", "a.pdf", set()) is True
