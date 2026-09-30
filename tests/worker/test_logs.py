import json
import logging
import re

import pytest
import structlog

from archive_common import logs


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level, {n: logging.getLogger(n).level for n in logs.QUIET}
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


def test_setup_adds_job_fields_and_quiets_libraries(capsys, restore_logging):
    logs.setup("info", "json")
    logging.LoggerAdapter(logging.getLogger("archive_worker.job"), {"job": 42, "step": None}).info("step %s", "split")
    logging.getLogger("archive_worker").info("plain")
    logging.getLogger("httpx").info("hidden")
    structlog.get_logger("archive_api").info("api.started", port=8080)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(r["logger"], r["event"]) for r in lines] == [
        ("archive_worker.job", "step split"), ("archive_worker", "plain"), ("archive_api", "api.started")]
    assert (lines[0]["job"], lines[0]["level"]) == (42, "info")
    assert lines[2]["port"] == 8080


def test_console_format(capsys, restore_logging):
    logs.setup("INFO")
    logging.LoggerAdapter(logging.getLogger("archive_worker.job"), {"job": 42}).info("step split")
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)  # colours, when the terminal has them
    assert "step split" in out and "job=42" in out


def test_job_event_log_gets_info_even_when_stdout_is_quieter(capsys, restore_logging):
    from archive_worker.events import JobEvents

    logs.setup("warning", "json")
    job_logger = logging.getLogger("archive_worker.job")
    saved_level = job_logger.level
    rec = JobEvents()
    rec.install()
    try:
        logging.LoggerAdapter(job_logger, {"job": 7, "step": "split"}).info("cut part 1")
    finally:
        rec.uninstall()
        job_logger.setLevel(saved_level)
    assert capsys.readouterr().out == ""
    [event] = rec._pending
    assert (event["job_id"], event["level"], event["step"], event["message"]) == (7, "info", "split", "cut part 1")
