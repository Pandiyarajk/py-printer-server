"""Print job submission, devmode application, polling and archiving.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import shutil
import threading
import time
import uuid
from ctypes import byref, c_ulong, cast, create_string_buffer, get_last_error
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from py_printer_server import winspool
from py_printer_server.printroute import PrintRoute, route

logger = logging.getLogger("printer_server")

# A4 at a typical 10-pitch monospace font: comfortable margins, not a driver
# guarantee, just a fixed layout for text we write to the spooler ourselves.
TEXT_COLS = 80
TEXT_LINES_PER_PAGE = 66

# How long to wait for a shell-verb job to appear in the queue before
# reporting it as failed to start. Fire-and-forget handlers can take a moment
# to spin up (Word/Acrobat cold start), so this is generous.
JOB_APPEAR_TIMEOUT = 20.0
JOB_POLL_INTERVAL = 0.5


class PrintError(RuntimeError):
    """Raised when a job cannot be printed at all (no route, bad printer)."""


@dataclass
class PrintOptions:
    printer: str
    color: bool = True
    paper: str = "A4"
    copies: int = 1
    duplex: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JobFile:
    name: str
    status: str = "queued"  # queued | printing | done | error | unsupported
    detail: str = ""


@dataclass
class Job:
    id: str
    files: list[JobFile]
    options: PrintOptions
    created: float = field(default_factory=time.time)
    status: str = "queued"  # queued | printing | done | error
    finished: float | None = None


class _DevmodeGuard:
    """Context manager that applies print options to a printer's devmode and
    restores the previous devmode on exit, including on an exception.

    The Win32 devmode is effectively global per-printer while it is set: two
    concurrent jobs on the same printer would fight over it if this were not
    serialised, which is why the caller (JobQueue) only ever runs one job at a
    time. A crash between apply and restore would otherwise leave the user's
    printer stuck on whatever the last job requested (e.g. permanently mono),
    so restoration happens in `__exit__`, not at the end of the happy path.
    """

    def __init__(self, printer_name: str, options: PrintOptions):
        self._printer_name = printer_name
        self._options = options
        self._handle = None
        self._previous_devmode_bytes: bytes | None = None

    def __enter__(self) -> None:
        self._handle = winspool.open_printer(self._printer_name, winspool.PRINTER_ALL_ACCESS)
        current = self._get_devmode()
        self._previous_devmode_bytes = bytes(current)
        self._apply(current)
        self._set_devmode(current)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._previous_devmode_bytes is not None and self._handle:
                restored = winspool.DEVMODEW.from_buffer_copy(self._previous_devmode_bytes)
                try:
                    self._set_devmode(restored)
                except winspool.WinspoolError:
                    logger.exception("failed to restore devmode for %s", self._printer_name)
        finally:
            if self._handle:
                winspool.close_printer(self._handle)
                self._handle = None

    def _get_devmode(self) -> winspool.DEVMODEW:
        needed = c_ulong(0)
        ok = winspool.winspool.GetPrinterW(self._handle, 2, None, 0, byref(needed))
        if not ok and get_last_error() != winspool.ERROR_INSUFFICIENT_BUFFER:
            raise winspool.WinspoolError(f"GetPrinterW sizing failed: {get_last_error()}")
        buf = create_string_buffer(needed.value)
        ok = winspool.winspool.GetPrinterW(self._handle, 2, buf, needed.value, byref(needed))
        if not ok:
            raise winspool.WinspoolError(f"GetPrinterW failed: {get_last_error()}")
        info = cast(buf, ctypes.POINTER(winspool.PRINTER_INFO_2W)).contents
        if not info.pDevMode:
            raise winspool.WinspoolError(f"printer {self._printer_name!r} returned no DEVMODE")
        # Copy the DEVMODEW out of the PRINTER_INFO_2W buffer immediately: the
        # `buf` bytes here go out of scope at the end of this method, and (as
        # with EnumPrintersW) a struct's embedded pointer is only valid while
        # its backing buffer is alive.
        return winspool.DEVMODEW.from_buffer_copy(info.pDevMode.contents)

    def _apply(self, dm: winspool.DEVMODEW) -> None:
        opts = self._options
        dm.dmColor = winspool.DMCOLOR_COLOR if opts.color else winspool.DMCOLOR_MONOCHROME
        dm.dmPaperSize = winspool.DMPAPER_A4 if opts.paper.upper() == "A4" else dm.dmPaperSize
        dm.dmCopies = max(1, opts.copies)
        dm.dmDuplex = winspool.DMDUP_VERTICAL if opts.duplex else winspool.DMDUP_SIMPLEX
        # dmFields is the trap: a field written above without its bit set
        # here is silently ignored by the driver. OR in, never overwrite, so
        # any bits the driver already had set for fields we do not touch
        # survive.
        dm.dmFields |= (
            winspool.DM_COLOR | winspool.DM_PAPERSIZE | winspool.DM_COPIES | winspool.DM_DUPLEX
        )

    def _set_devmode(self, dm: winspool.DEVMODEW) -> None:
        info = winspool.PRINTER_INFO_2W()
        # SetPrinterW(level=2) with only pDevMode populated updates just the
        # devmode; every other pointer field must stay NULL or the call
        # attempts to rewrite fields we never read, which can fail or (worse)
        # silently blank them.
        ctypes.memset(byref(info), 0, ctypes.sizeof(info))
        info.pDevMode = ctypes.pointer(dm)
        ok = winspool.winspool.SetPrinterW(self._handle, 2, byref(info), 0)
        if not ok:
            raise winspool.WinspoolError(f"SetPrinterW failed: {get_last_error()}")


def print_text(src: Path, options: PrintOptions) -> None:
    """Print a plain text file by writing it straight to the spooler.

    This is the one path we fully control: pagination, line wrapping and
    the raw bytes sent are all ours, so this is also the path where
    colour/mono, copies and duplex are guaranteed to apply exactly as
    requested (via the devmode set on the printer before StartDocPrinterW).
    """
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PrintError(f"cannot read {src.name}: {exc}") from exc

    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        for i in range(0, len(raw_line), TEXT_COLS):
            lines.append(raw_line[i:i + TEXT_COLS])

    pages: list[str] = []
    for i in range(0, len(lines), TEXT_LINES_PER_PAGE):
        page_lines = lines[i:i + TEXT_LINES_PER_PAGE]
        pages.append("\r\n".join(page_lines) + "\r\n\f")

    handle = winspool.open_printer(options.printer, winspool.PRINTER_ALL_ACCESS)
    try:
        doc_info = winspool.DOC_INFO_1W(pDocName=src.name, pOutputFile=None, pDatatype="RAW")
        job_id = winspool.winspool.StartDocPrinterW(handle, 1, byref(doc_info))
        if not job_id:
            raise winspool.WinspoolError(f"StartDocPrinterW failed: {get_last_error()}")
        try:
            for _ in range(max(1, options.copies)):
                for page in pages:
                    if not winspool.winspool.StartPagePrinter(handle):
                        raise winspool.WinspoolError(f"StartPagePrinter failed: {get_last_error()}")
                    try:
                        data = page.encode("utf-8", errors="replace")
                        written = c_ulong(0)
                        buf = create_string_buffer(data, len(data))
                        if not winspool.winspool.WritePrinter(handle, buf, len(data), byref(written)):
                            raise winspool.WinspoolError(f"WritePrinter failed: {get_last_error()}")
                    finally:
                        winspool.winspool.EndPagePrinter(handle)
        finally:
            winspool.winspool.EndDocPrinter(handle)
    finally:
        winspool.close_printer(handle)


def print_via_shell(src: Path, options: PrintOptions) -> None:
    """Print via the shell's ``printto`` verb, with the devmode applied first.

    Fire-and-forget: this returns once the associated application has been
    launched to print, not once the page has actually come out. Job
    completion is observed separately by polling EnumJobsW (see
    JobQueue._process_one), because the shell call itself reports nothing
    beyond "a handler was found and started".
    """
    code = winspool.shell_print_to(options.printer, str(src))
    if code <= 32:
        raise PrintError(
            f"no registered handler could print {src.name} "
            f"(ShellExecute error code {code})"
        )


class JobQueue:
    """Single-worker print queue.

    One worker thread, so two phones uploading and printing at the same time
    do not interleave devmode changes on the same printer -- the devmode is
    briefly a global setting while a shell-verb job is in flight, and this is
    what keeps that window safe.
    """

    def __init__(self, jobs_dir: Path, dry_run: bool = False):
        self.jobs_dir = jobs_dir
        self.dry_run = dry_run
        self._queue: queue.Queue[Job] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True, name="print-worker")
        self._worker.start()

    def submit(self, spool_dir: Path, filenames: list[str], options: PrintOptions) -> Job:
        job_id = uuid.uuid4().hex[:12]
        files = [JobFile(name=name) for name in filenames]
        job = Job(id=job_id, files=files, options=options)
        with self._lock:
            self._jobs[job_id] = job
        self._queue.put((job, spool_dir))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return jobs[:limit]

    def _run(self) -> None:
        while True:
            job, spool_dir = self._queue.get()
            try:
                self._process(job, spool_dir)
            except Exception:
                logger.exception("unhandled error processing job %s", job.id)
                job.status = "error"
                job.finished = time.time()

    def _process(self, job: Job, spool_dir: Path) -> None:
        job.status = "printing"
        # Every route runs under the same devmode guard: RAW_TEXT jobs still
        # benefit from it (copies/duplex/color are applied by the printer
        # itself as it renders the raw data), and SHELL_VERB jobs need it
        # applied before the handler launches.
        try:
            if self.dry_run:
                self._process_dry_run(job, spool_dir)
            else:
                with _DevmodeGuard(job.options.printer, job.options):
                    for jf in job.files:
                        self._process_one(job, spool_dir, jf)
        finally:
            job.status = "error" if any(f.status == "error" for f in job.files) else "done"
            job.finished = time.time()
            self._archive(job, spool_dir)

    def _process_dry_run(self, job: Job, spool_dir: Path) -> None:
        for jf in job.files:
            src = spool_dir / jf.name
            decision = route(src)
            logger.info(
                "DRY-RUN would print %s via %s printer=%r color=%s paper=%s copies=%d duplex=%s (%s)",
                jf.name, decision.route.value, job.options.printer, job.options.color,
                job.options.paper, job.options.copies, job.options.duplex, decision.reason,
            )
            jf.status = "done" if decision.route != PrintRoute.UNSUPPORTED else "unsupported"
            jf.detail = decision.reason

    def _process_one(self, job: Job, spool_dir: Path, jf: JobFile) -> None:
        src = spool_dir / jf.name
        decision = route(src)
        if decision.route == PrintRoute.UNSUPPORTED:
            jf.status = "unsupported"
            jf.detail = decision.reason
            return
        try:
            submitted_after = time.time()
            if decision.route == PrintRoute.RAW_TEXT:
                print_text(src, job.options)
            else:
                print_via_shell(src, job.options)
                self._wait_for_shell_job(job.options.printer, src.name, submitted_after)
            jf.status = "done"
        except (PrintError, winspool.WinspoolError) as exc:
            jf.status = "error"
            jf.detail = str(exc)
            logger.warning("print failed for %s: %s", jf.name, exc)

    def _wait_for_shell_job(self, printer: str, doc_name: str, submitted_after: float) -> None:
        """Best-effort wait for a shell-verb job to leave the spooler queue.

        Neither ShellExecuteW nor the handler it launches reports a usable
        job id back to us, so this matches on document name within the
        printer's current job list. A job that never appears is logged, not
        raised -- the file may still have printed correctly with a handler
        that renames the job, so treat "not observed" as inconclusive rather
        than as a failure.
        """
        deadline = time.time() + JOB_APPEAR_TIMEOUT
        seen = False
        while time.time() < deadline:
            jobs = self._enum_jobs(printer)
            matching = [j for j in jobs if doc_name in j]
            if matching:
                seen = True
            elif seen:
                # It appeared and then left the queue: done.
                return
            time.sleep(JOB_POLL_INTERVAL)
        if not seen:
            logger.info(
                "job for %s never appeared in %s's queue within %.0fs; "
                "it may still have printed via a handler that renamed it",
                doc_name, printer, JOB_APPEAR_TIMEOUT,
            )

    def _enum_jobs(self, printer: str) -> list[str]:
        handle = winspool.open_printer(printer)
        try:
            needed = c_ulong(0)
            returned = c_ulong(0)
            ok = winspool.winspool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, None, 0, byref(needed), byref(returned))
            if not ok and get_last_error() != winspool.ERROR_INSUFFICIENT_BUFFER:
                return []
            if needed.value == 0:
                return []
            buf = create_string_buffer(needed.value)
            ok = winspool.winspool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, buf, needed.value, byref(needed), byref(returned))
            if not ok:
                return []
            array_type = winspool.JOB_INFO_1W * returned.value
            array = cast(buf, ctypes.POINTER(array_type)).contents
            return [j.pDocument for j in array if j.pDocument]
        finally:
            winspool.close_printer(handle)

    def _archive(self, job: Job, spool_dir: Path) -> None:
        """Move every source file out of the spool into a per-job archive
        folder, alongside a job.json recording the outcome. Printed files are
        moved, not deleted, so a reprint does not require re-uploading."""
        stamp = datetime.fromtimestamp(job.created).strftime("%Y%m%d-%H%M%S")
        dest = self.jobs_dir / f"print-job-{stamp}-{job.id}"
        dest.mkdir(parents=True, exist_ok=True)

        for jf in job.files:
            src = spool_dir / jf.name
            if not src.exists():
                continue
            target = dest / jf.name
            counter = 2
            while target.exists():
                target = dest / f"{target.stem} ({counter}){target.suffix}"
                counter += 1
            try:
                shutil.move(str(src), str(target))
            except OSError as exc:
                logger.warning("could not archive %s: %s", src, exc)

        record = {
            "id": job.id,
            "created": job.created,
            "finished": job.finished,
            "status": job.status,
            "options": job.options.to_dict(),
            "files": [asdict(f) for f in job.files],
        }
        try:
            (dest / "job.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not write job.json for %s: %s", job.id, exc)
