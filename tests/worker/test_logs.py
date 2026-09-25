import logging

import pytest

from archive_common import logs


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level, {n: logging.getLogger(n).level for n in logs.QUIET}
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)


def test_setup_formats_job_records_and_quiets_libraries(capsys, restore_logging):
    logs.setup("info")
    logging.LoggerAdapter(logging.getLogger("archive_worker.job"), {"job": 42}).info("step %s", "split")
    logging.getLogger("archive_worker").info("plain")
    logging.getLogger("httpx").info("hidden")
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2
    assert err[0].endswith("INFO    archive_worker.job: [job 42] step split")
    assert err[1].endswith("INFO    archive_worker: plain")


def test_job_event_log_gets_info_even_when_stderr_is_quieter(capsys, restore_logging):
    from archive_worker.events import JobEvents

    logs.setup("warning")
    job_logger = logging.getLogger("archive_worker.job")
    saved_level = job_logger.level
    rec = JobEvents()
    rec.install()
    try:
        logging.LoggerAdapter(job_logger, {"job": 7, "step": "split"}).info("cut part 1")
    finally:
        rec.uninstall()
        job_logger.setLevel(saved_level)
    assert capsys.readouterr().err == ""
    [event] = rec._pending
    assert (event["job_id"], event["level"], event["step"], event["message"]) == (7, "info", "split", "cut part 1")
