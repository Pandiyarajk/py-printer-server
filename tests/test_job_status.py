"""Tests for job status honesty, archiving policy and pagination.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

The central rule under test: a job reported "done" must mean paper actually
came out. An earlier version reported DONE for jobs that printed nothing.
"""

from __future__ import annotations

import json
from pathlib import Path


from py_printer_server.printing import (
    Job,
    JobFile,
    JobQueue,
    PrintOptions,
    paginate_text,
    TEXT_COLS,
    TEXT_LINES_PER_PAGE,
)


def _job(statuses: list[str]) -> Job:
    return Job(
        id="j1",
        files=[JobFile(name=f"f{i}.txt", status=s) for i, s in enumerate(statuses)],
        options=PrintOptions(printer="P"),
    )


class TestOverallStatus:
    def test_all_done_is_done(self) -> None:
        assert JobQueue._overall_status(_job(["done", "done"])) == "done"

    def test_any_error_is_error(self) -> None:
        assert JobQueue._overall_status(_job(["done", "error"])) == "error"

    def test_all_unsupported_is_not_done(self) -> None:
        """Regression: a job whose every file was unsupported printed nothing
        but reported DONE."""
        assert JobQueue._overall_status(_job(["unsupported"])) == "unsupported"

    def test_still_queued_is_error_not_done(self) -> None:
        """Regression: when the printer could not be opened, no file was ever
        touched, so all files stayed 'queued' and the job reported DONE."""
        assert JobQueue._overall_status(_job(["queued", "queued"])) == "error"

    def test_mixed_done_and_unsupported_is_partial(self) -> None:
        assert JobQueue._overall_status(_job(["done", "unsupported"])) == "partial"


class TestArchivePolicy:
    def test_only_printed_files_are_archived(self, tmp_path: Path) -> None:
        """A file that failed stays in the spool so it can be retried without
        re-uploading from the phone."""
        spool = tmp_path / "spool"
        jobs = tmp_path / "jobs"
        spool.mkdir()
        jobs.mkdir()
        for name in ("ok.txt", "bad.txt", "weird.xyz"):
            (spool / name).write_text("x", encoding="utf-8")

        job = Job(
            id="j2",
            files=[
                JobFile(name="ok.txt", status="done"),
                JobFile(name="bad.txt", status="error", detail="printer on fire"),
                JobFile(name="weird.xyz", status="unsupported", detail="no handler"),
            ],
            options=PrintOptions(printer="P"),
        )
        job.status = "partial"

        queue = JobQueue(jobs, dry_run=True)
        queue._archive(job, spool)

        assert not (spool / "ok.txt").exists(), "printed file should be archived"
        assert (spool / "bad.txt").exists(), "failed file must stay for retry"
        assert (spool / "weird.xyz").exists(), "unsupported file must stay for retry"

        archive = next(jobs.iterdir())
        assert (archive / "ok.txt").exists()
        assert not (archive / "bad.txt").exists()

    def test_job_json_records_failure_detail(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        jobs = tmp_path / "jobs"
        spool.mkdir()
        jobs.mkdir()

        job = _job(["error"])
        job.files[0].detail = "SetPrinterW failed"
        job.status = "error"
        job.error = "could not open printer"

        JobQueue(jobs, dry_run=True)._archive(job, spool)

        record = json.loads((next(jobs.iterdir()) / "job.json").read_text(encoding="utf-8"))
        assert record["status"] == "error"
        assert record["error"] == "could not open printer"
        assert record["files"][0]["detail"] == "SetPrinterW failed"


class TestPagination:
    def test_short_text_is_one_page(self) -> None:
        pages = paginate_text("hello\nworld")
        assert len(pages) == 1
        assert pages[0].endswith("\f")

    def test_long_lines_wrap_at_column_limit(self) -> None:
        pages = paginate_text("x" * (TEXT_COLS * 2))
        body = pages[0].replace("\f", "")
        for line in body.split("\r\n"):
            assert len(line) <= TEXT_COLS

    def test_page_breaks_at_line_limit(self) -> None:
        text = "\n".join(str(i) for i in range(TEXT_LINES_PER_PAGE + 5))
        assert len(paginate_text(text)) == 2

    def test_empty_text_still_produces_a_page(self) -> None:
        """An empty file yields one blank page rather than no pages at all,
        so the job has something to send and does not silently do nothing."""
        pages = paginate_text("")
        assert len(pages) == 1
        assert pages[0].endswith("\f")
        assert pages[0].replace("\r\n", "").replace("\f", "") == ""
