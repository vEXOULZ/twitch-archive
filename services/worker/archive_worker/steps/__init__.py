"""Step registry: job kinds in ``jobs.KINDS`` are sequences of these names."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..context import JobContext
from .capture import capture_step, live_record
from .media import cleanup, dmca_edit, ensure_source, finalize, resolve_vod, split
from .metadata import chapters, chat, emotes, logs_manual
from .publish import describe, upload

Step = Callable[[JobContext], Awaitable[None]]

STEPS: dict[str, Step] = {
    "capture": capture_step,
    "live_record": live_record,
    "resolve_vod": resolve_vod,
    "finalize": finalize,
    "ensure_source": ensure_source,
    "chapters": chapters,
    "chat": chat,
    "emotes": emotes,
    "logs_manual": logs_manual,
    "split": split,
    "dmca_edit": dmca_edit,
    "upload": upload,
    "describe": describe,
    "cleanup": cleanup,
}
