"""Print job submission, devmode application, polling and archiving.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

import ctypes
import json
import logging
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
# Polled faster than JOB_POLL_INTERVAL while waiting for a shell-launched job
# to *appear*: that job is only ever identified by catching it in the queue, so
# a short job slipping between two polls would be reported as never printed.
# Once it has been seen, the drain wait backs off to JOB_POLL_INTERVAL.
JOB_APPEAR_POLL_INTERVAL = 0.1


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
    # queued | printing | done | partial | unsupported | error
    status: str = "queued"
    finished: float | None = None
    error: str = ""


class PrinterSession:
    """An open printer handle plus a per-job DEVMODE built from PrintOptions.

    Per-job, deliberately. The intuitive approach -- GetPrinterW, edit the
    devmode, SetPrinterW -- was what an earlier version of this file did, and
    it is wrong twice over: it needs the "Manage this printer" permission that
    an ordinary user does not have (ERROR_ACCESS_DENIED), and even with rights
    it rewrites the printer's *global* defaults, so one web request would
    change what every other application on the machine prints. Building the
    devmode with DocumentPropertiesW instead needs no privileges and touches
    nothing outside this document, which also means there is no global state
    to restore afterwards and no window in which a crash could strand the
    printer in mono.
    """

    def __init__(self, printer_name: str, options: PrintOptions):
        self._printer_name = printer_name
        self._options = options
        self._handle = None
        self.devmode = None
        self._devmode_buf = None

    def __enter__(self) -> "PrinterSession":
        # __exit__ is NOT called when __enter__ raises, so anything acquired
        # here must be released on the failure path explicitly or it leaks for
        # the life of the (long-lived) worker thread.
        self._handle = winspool.open_printer(self._printer_name, winspool.PRINTER_ACCESS_USE)
        try:
            self.devmode, self._devmode_buf = winspool.build_job_devmode(
                self._handle, self._printer_name, self._apply
            )
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._close()

    @property
    def handle(self):
        return self._handle

    def _close(self) -> None:
        self.devmode = None
        self._devmode_buf = None
        if self._handle:
            winspool.close_printer(self._handle)
            self._handle = None

    def _apply(self, dm: winspool.DEVMODEW) -> None:
        opts = self._options
        dm.dmColor = winspool.DMCOLOR_COLOR if opts.color else winspool.DMCOLOR_MONOCHROME
        if opts.paper.upper() == "A4":
            dm.dmPaperSize = winspool.DMPAPER_A4
        # dmCopies is the ONLY place copies are applied. print_text must not
        # also loop its pages, or the two multiply: an earlier version did
        # both, so 3 copies of a 2-page file emitted 18 pages.
        dm.dmCopies = max(1, opts.copies)
        dm.dmDuplex = winspool.DMDUP_VERTICAL if opts.duplex else winspool.DMDUP_SIMPLEX
        # dmFields is the trap: a field written above without its bit set here
        # is silently ignored by the driver. OR in, never overwrite, so bits
        # the driver already set for fields we do not touch survive.
        dm.dmFields |= (
            winspool.DM_COLOR | winspool.DM_PAPERSIZE | winspool.DM_COPIES | winspool.DM_DUPLEX
        )


def paginate_text(text: str) -> list[str]:
    """Wrap and paginate plain text into fixed-size pages.

    Pure function, so the pagination can be tested without a printer.
    """
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
    return pages or ["\f"]


def print_text(src: Path, session: "PrinterSession") -> int:
    """Print a plain text file by writing it through the spooler.

    Returns the spooler job id so the caller can wait for it to actually
    drain from the printer's queue -- StartDocPrinterW/WritePrinter/
    EndDocPrinter only confirm the data reached the spooler, not that paper
    came out. An earlier version marked the file "done" as soon as this
    function returned, which could report success while the job was still
    sitting in (or being printed from) the Windows print queue.

    Copies are NOT looped here: the session's devmode already carries
    dmCopies, and doing both multiplies them.

    The document datatype is ``TEXT``, not ``RAW``. RAW means "these bytes are
    already in the printer's own language" -- sending plain text as RAW to a
    PCL or host-based inkjet prints nothing, or pages of garbage. TEXT asks the
    spooler's print processor to render the characters for the device. For the
    same reason the bytes are encoded as cp1252 rather than UTF-8: the TEXT
    processor expects an ANSI code page, and a UTF-8 multi-byte sequence would
    arrive as mojibake.
    """
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PrintError(f"cannot read {src.name}: {exc}") from exc

    pages = paginate_text(text)
    handle = session.handle

    doc_info = winspool.DOC_INFO_1W(pDocName=src.name, pOutputFile=None, pDatatype="TEXT")
    job_id = winspool.winspool.StartDocPrinterW(handle, 1, byref(doc_info))
    if not job_id:
        raise winspool.WinspoolError(
            f"StartDocPrinterW failed for {src.name}: {get_last_error()}"
        )
    try:
        for page in pages:
            if not winspool.winspool.StartPagePrinter(handle):
                raise winspool.WinspoolError(f"StartPagePrinter failed: {get_last_error()}")
            try:
                data = page.encode("cp1252", errors="replace")
                written = c_ulong(0)
                buf = create_string_buffer(data, len(data))
                if not winspool.winspool.WritePrinter(handle, buf, len(data), byref(written)):
                    raise winspool.WinspoolError(f"WritePrinter failed: {get_last_error()}")
                if written.value != len(data):
                    raise winspool.WinspoolError(
                        f"WritePrinter wrote {written.value} of {len(data)} bytes"
                    )
            finally:
                winspool.winspool.EndPagePrinter(handle)
    finally:
        winspool.winspool.EndDocPrinter(handle)
    return job_id


def print_via_shell(src: Path, options: PrintOptions) -> None:
    """Print via the shell's ``printto`` verb.

    Fire-and-forget: this returns once the associated application has been
    launched to print, not once the page has actually come out. Job completion
    is observed separately by polling EnumJobsW (see JobQueue._process_one),
    because the shell call itself reports nothing beyond "a handler was found
    and started".

    Note this path cannot carry our per-job devmode: the handler builds its
    own. Colour/duplex/copies therefore follow the printer's standing defaults
    for PDFs, images and Office documents -- a real limitation, surfaced to the
    user rather than silently ignored.
    """
    code = winspool.shell_print_to(options.printer, str(src))
    if code <= 32:
        raise PrintError(
            f"no program on this PC could print {src.name} "
            f"(ShellExecute error code {code}"
            + (", no application is associated with this file type"
               if code == winspool.SE_ERR_NOASSOC else "")
            + ")"
        )


class JobQueue:
    """Single-worker print queue.

    One worker thread, so two phones printing at the same time cannot
    interleave their spooler calls on the same printer handle.
    """

    def __init__(self, jobs_dir: Path, dry_run: bool = False):
        self.jobs_dir = jobs_dir
        self.dry_run = dry_run
        self._queue: queue.Queue[tuple[Job, Path]] = queue.Queue()
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
            # Everything, including the unpack, sits inside the try: an
            # exception escaping this loop would kill the only worker thread
            # for the life of the process, after which every submitted job
            # would sit at "queued" forever with nothing logged.
            item = None
            try:
                item = self._queue.get()
                job, spool_dir = item
                self._process(job, spool_dir)
            except Exception:
                job_id = getattr(item[0], "id", "<unknown>") if isinstance(item, tuple) and item else "<unknown>"
                logger.exception("unhandled error processing job %s", job_id)
                if isinstance(item, tuple) and item and isinstance(item[0], Job):
                    failed = item[0]
                    failed.status = "error"
                    if not failed.error:
                        failed.error = "internal error; see the server log"
                    for jf in failed.files:
                        if jf.status in ("queued", "printing"):
                            jf.status = "error"
                            jf.detail = jf.detail or "job aborted"
                    failed.finished = time.time()

    def _process(self, job: Job, spool_dir: Path) -> None:
        job.status = "printing"
        try:
            if self.dry_run:
                self._process_dry_run(job, spool_dir)
            else:
                with PrinterSession(job.options.printer, job.options) as session:
                    for jf in job.files:
                        self._process_one(job, spool_dir, jf, session)
        except (PrintError, winspool.WinspoolError, OSError) as exc:
            # A failure opening the printer or building the devmode happens
            # before any file is touched. Without marking the files here they
            # would still read "queued", and the status rule below would then
            # call a job that never reached the printer a success.
            job.error = str(exc)
            for jf in job.files:
                if jf.status in ("queued", "printing"):
                    jf.status = "error"
                    jf.detail = str(exc)
            logger.warning("job %s failed before printing: %s", job.id, exc)
        finally:
            job.status = self._overall_status(job)
            job.finished = time.time()
            self._archive(job, spool_dir)

    @staticmethod
    def _overall_status(job: Job) -> str:
        """Derive the job's status from its files, erring towards honesty.

        Anything that is not an outright success makes the job non-"done": a
        job reported as done must mean paper actually came out. An earlier
        version only checked for "error", so a job whose files were all
        "unsupported" -- or all still "queued" after an early failure --
        reported DONE while printing nothing.
        """
        statuses = {f.status for f in job.files}
        if not statuses or statuses == {"done"}:
            return "done"
        if "error" in statuses or "queued" in statuses or "printing" in statuses:
            return "error"
        if statuses == {"unsupported"}:
            return "unsupported"
        return "partial"

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

    def _process_one(self, job: Job, spool_dir: Path, jf: JobFile, session: "PrinterSession") -> None:
        src = spool_dir / jf.name
        if not src.is_file():
            jf.status = "error"
            jf.detail = "file is no longer in the spool"
            return
        decision = route(src)
        if decision.route == PrintRoute.UNSUPPORTED:
            jf.status = "unsupported"
            jf.detail = decision.reason
            return
        try:
            if decision.route == PrintRoute.RAW_TEXT:
                job_id = print_text(src, session)
                if not self._wait_for_job_id(job.options.printer, job_id):
                    # Still queued at the deadline is not a failure: the job
                    # demonstrably reached the spooler (StartDocPrinterW gave
                    # us this id and every page was written), so it is a large
                    # job still printing, not a lost one. Calling that an error
                    # would leave the file in the spool inviting a retry, and
                    # the retry prints it a second time. The shell path below
                    # treats the same state as success for the same reason.
                    logger.info(
                        "%s is still on %s's queue %.0fs after spooling; "
                        "not waiting further (the job may simply be large)",
                        src.name, job.options.printer, JOB_APPEAR_TIMEOUT,
                    )
            else:
                queue_before = self._enum_job_ids(job.options.printer)
                print_via_shell(src, job.options)
                if not self._wait_for_shell_job(job.options.printer, src.name, queue_before):
                    raise PrintError(
                        f"{src.name} was handed to its shell handler, but no print job for it "
                        f"ever reached the spooler within {JOB_APPEAR_TIMEOUT:.0f}s -- the handler "
                        "may be stuck on a dialog (e.g. a first-run prompt) rather than printing"
                    )
            jf.status = "done"
        except (PrintError, winspool.WinspoolError, OSError) as exc:
            jf.status = "error"
            jf.detail = str(exc)
            logger.warning("print failed for %s: %s", jf.name, exc)

    def _wait_for_shell_job(self, printer: str, doc_name: str, ids_before: set[int]) -> bool:
        """Best-effort wait for a shell-launched job to reach, then drain from,
        the queue. Returns False if no new job was ever observed.

        Neither ShellExecuteW nor the handler it launches reports the job id
        back, so the job has to be spotted by diffing the queue. That diff is
        taken over spooler job *ids*, not document names: handlers rename the
        spool document freely (Word prefixes it, Acrobat may use a full path),
        and -- worse -- a name that already appears in the baseline hides the
        new job completely, which happens routinely when the same file is
        printed twice. Job ids are unique per spooler, so neither confuses it.

        ShellExecuteW only launches the handler; it returns long before that
        process has actually spooled anything, so the very first poll -- taken
        immediately after launch -- almost always sees no new job yet. Treating
        "nothing new in the queue" as done at that point (an earlier version
        did exactly this) reports success for a job that was never sent to the
        printer at all, e.g. a cold-starting reader stuck behind a hidden
        first-run dialog. So a new job must be *observed* at least once before
        its later absence is read as "finished printing" -- only then does an
        empty diff mean done rather than not-yet-started.

        The flip side of requiring that observation is a job finishing between
        two polls, which would never be seen and so be reported as a failure
        even though it printed. Hence the tight poll while waiting for it to
        appear (JOB_APPEAR_POLL_INTERVAL): once it has been seen there is
        nothing left to race with, so the wait for it to drain backs off to
        the slower JOB_POLL_INTERVAL.
        """
        baseline = set(ids_before)
        deadline = time.time() + JOB_APPEAR_TIMEOUT
        ours: set[int] = set()
        while time.time() < deadline:
            current = self._enum_job_ids(printer)
            ours |= current - baseline
            # Wait for *our* ids to leave, not for the queue to go quiet: a
            # job another application queues meanwhile is not ours to wait on.
            if ours and not (ours & current):
                return True
            time.sleep(JOB_POLL_INTERVAL if ours else JOB_APPEAR_POLL_INTERVAL)
        if ours:
            logger.info(
                "queue for %s still busy %.0fs after launching %s; "
                "not waiting further (the job may simply be large)",
                printer, JOB_APPEAR_TIMEOUT, doc_name,
            )
            return True
        logger.warning(
            "no print job for %s ever appeared on %s within %.0fs after launching its handler",
            doc_name, printer, JOB_APPEAR_TIMEOUT,
        )
        return False

    def _wait_for_job_id(self, printer: str, job_id: int, timeout: float = JOB_APPEAR_TIMEOUT) -> bool:
        """Wait for a specific spooler job id to drain from the queue.

        Unlike _wait_for_shell_job, there is no "did it ever appear?"
        ambiguity here: StartDocPrinterW already returned this exact job id,
        so its absence at any point means it finished (printed, or was
        cancelled/errored out on the printer's side) -- not that it never
        started.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job_id not in self._enum_job_ids(printer):
                return True
            time.sleep(JOB_POLL_INTERVAL)
        return job_id not in self._enum_job_ids(printer)

    def _enum_job_ids(self, printer: str) -> set[int]:
        """Return the spooler job id of every job currently on `printer`.

        Ids, not document names: a name is neither unique (printing the same
        file twice puts two identically named jobs on the queue) nor stable
        (a shell handler renames the spool document as it likes), and both
        wait loops above identify a job purely by diffing this set.

        JobId is a c_ulong, so each value is copied into a plain int on
        access and nothing here outlives `buf`. Returning the JOB_INFO_1W
        structs instead would not be safe: their string pointers reference
        this function's buffer, freed on return -- the exact bug that once
        produced garbage printer names from EnumPrintersW.
        """
        handle = winspool.open_printer(printer)
        try:
            needed = c_ulong(0)
            returned = c_ulong(0)
            ok = winspool.winspool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, None, 0, byref(needed), byref(returned))
            if not ok and get_last_error() != winspool.ERROR_INSUFFICIENT_BUFFER:
                return set()
            if needed.value == 0:
                return set()
            buf = create_string_buffer(needed.value)
            ok = winspool.winspool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, buf, needed.value, byref(needed), byref(returned))
            if not ok:
                return set()
            array_type = winspool.JOB_INFO_1W * returned.value
            array = cast(buf, ctypes.POINTER(array_type)).contents
            return {j.JobId for j in array}
        finally:
            winspool.close_printer(handle)

    def _archive(self, job: Job, spool_dir: Path) -> None:
        """Move successfully printed files into a per-job archive folder.

        Only files with status "done" are moved. A file that errored or was
        unsupported stays in the spool so the user can fix the problem and
        retry it -- an earlier version archived everything, which meant a
        failed file silently vanished from the listing and had to be
        re-uploaded from the phone, the opposite of why files are moved
        rather than deleted.
        """
        stamp = datetime.fromtimestamp(job.created).strftime("%Y%m%d-%H%M%S")
        dest = self.jobs_dir / f"print-job-{stamp}-{job.id}"
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create archive folder %s: %s", dest, exc)
            return

        for jf in job.files:
            if jf.status != "done":
                continue
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
            "error": job.error,
            "options": job.options.to_dict(),
            "files": [asdict(f) for f in job.files],
        }
        try:
            (dest / "job.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not write job.json for %s: %s", job.id, exc)
