"""archive-worker entrypoint.

    archive-worker run [--dry-run]            monitor + job runner + admin API
    archive-worker import-youtube-token FILE  import youtube.auth from a legacy config.json
    archive-worker enqueue KIND VOD_ID [JSON] queue a job without the admin API
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path

import uvicorn

from archive_common import http, logs
from archive_common.config import get_settings
from archive_common.twitch.gql import Gql
from archive_common.twitch.helix import Helix

from . import jobs, youtube
from .admin import create_admin_app
from .context import Deps
from .monitor import Monitor

log = logging.getLogger("archive_worker")


async def serve(dry_run: bool = False) -> None:
    settings = get_settings()
    if dry_run:
        settings.dry_run = True
    deps = Deps(settings, Helix(settings), Gql(settings), youtube.YouTube(settings))
    runner = jobs.Runner(deps)
    monitor = Monitor(deps.helix, runner)
    admin = uvicorn.Server(
        uvicorn.Config(
            create_admin_app(deps, runner),
            host=settings.admin_host,
            port=settings.admin_port,
            log_level=settings.log_level.lower(),
            log_config=None,  # use the handler from logs.setup
            proxy_headers=False,
        )
    )
    admin.install_signal_handlers = lambda: None  # we handle signals below

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows dev boxes
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    log.info(
        "worker starting: channel=%s live_record=%s vod_download=%s youtube_upload=%s dry_run=%s",
        settings.twitch_username, settings.live_record, settings.vod_download,
        settings.youtube_upload, settings.dry_run,
    )
    tasks = [
        asyncio.create_task(runner.run_forever(), name="runner"),
        asyncio.create_task(monitor.run_forever(), name="monitor"),
    ]
    if settings.youtube_upload and settings.google_client_id:
        # Daily refresh: surfaces a revoked token in the logs before a stream needs
        # it, and keeps Google from revoking it after six months without uploads.
        tasks.append(asyncio.create_task(deps.youtube.keepalive(settings.youtube_keepalive_hours),
                                         name="youtube-keepalive"))
    tasks.append(asyncio.create_task(admin.serve(), name="admin"))  # keep last: shutdown awaits it
    stopper = asyncio.create_task(stop.wait())
    pending: set[asyncio.Task] = {*tasks, stopper}
    while True:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        crashed = [t for t in done if t is not stopper and t.exception()]
        for t in crashed:
            log.error("%s crashed", t.get_name(), exc_info=t.exception())
        # The monitor returns normally when Twitch credentials are missing; keep serving.
        if stopper in done or crashed or any(t.get_name() != "monitor" for t in done):
            break

    log.info("shutting down; interrupted jobs resume on next start")
    admin.should_exit = True  # uvicorn stops on its own; cancelling it logs a spurious traceback
    await runner.shutdown()
    admin_task, others = tasks[-1], tasks[:-1]
    for t in others:
        t.cancel()
    await asyncio.gather(*others, return_exceptions=True)
    try:
        await asyncio.wait_for(admin_task, timeout=10)
    except (TimeoutError, asyncio.CancelledError, Exception):
        pass
    await http.close_client()


async def _enqueue(kind: str, vod_id: str, payload: str | None) -> None:
    job = await jobs.enqueue(kind, vod_id, json.loads(payload) if payload else {})
    print(f"queued job {job.id} ({kind} {vod_id}); a running worker picks it up within 10s")


def run() -> None:
    parser = argparse.ArgumentParser(prog="archive-worker")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="run monitor, job runner and admin API (default)")
    p_run.add_argument("--dry-run", action="store_true", help="download and process, but never upload or delete")
    p_tok = sub.add_parser("import-youtube-token", help="import youtube.auth from the legacy config.json")
    p_tok.add_argument("config", type=Path)
    p_enq = sub.add_parser("enqueue", help="queue a job directly in the database")
    p_enq.add_argument("kind", choices=sorted(jobs.KINDS))
    p_enq.add_argument("vod_id")
    p_enq.add_argument("payload", nargs="?", help="JSON object, e.g. '{\"start_part\": 2}'")
    args = parser.parse_args()

    logs.setup(get_settings().log_level)
    if args.cmd == "import-youtube-token":
        asyncio.run(youtube.import_legacy_token(args.config))
        print("YouTube refresh token imported.")
    elif args.cmd == "enqueue":
        asyncio.run(_enqueue(args.kind, args.vod_id, args.payload))
    else:
        asyncio.run(serve(dry_run=getattr(args, "dry_run", False)))


if __name__ == "__main__":
    run()
