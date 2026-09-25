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
