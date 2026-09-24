"""Capture golden responses from the legacy Feathers API.

Usage:
    python tests/api_contract/capture_golden.py http://legacy-host:3030

Writes tests/api_contract/golden.json: a list of {path, status, body}. The
contract tests replay every path against archive-api and diff the bodies.
Comment cursors are followed a few pages so cursor encoding is covered too.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent

VOD_QUERIES = [
    "/vods?$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?$limit=20&$skip=20&$sort[createdAt]=-1",
    "/vods?$limit=10&$skip=50&$sort[createdAt]=-1",
    "/vods?$limit=100",  # clamped to paginate.max
    "/vods",
    "/vods?createdAt[$gte]=2024-10-01T00:00:00.000Z&createdAt[$lte]=2025-01-31T23:59:59.000Z&$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?title[$iLike]=%25stream%25&$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?title[$iLike]=%25DITCHER%25&$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?chapters[name]=risk&$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?chapters[name]=Just%20Chatting&$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?chapters[name]=zzzz-no-such-game&$limit=20&$skip=0&$sort[createdAt]=-1",
    "/vods?platform=twitch&$limit=5&$sort[createdAt]=1",
    "/vods?$select[]=id&$select[]=title&$limit=5&$sort[createdAt]=-1",
    "/vods/does-not-exist",
    "/games",
    "/streams",
    "/nope",
]

COMMENT_OFFSETS = [0, 1, 600, 3000.5]
CURSOR_PAGES = 3


def main(base: str) -> None:
    client = httpx.Client(base_url=base.rstrip("/"), timeout=30)
    out: list[dict] = []

    def get(path: str) -> httpx.Response:
        time.sleep(0.35)  # legacy limiter: 20 req / 5 s
        r = client.get(path)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        out.append({"path": path, "status": r.status_code, "body": body})
        print(r.status_code, path)
        return r

    for q in VOD_QUERIES:
        get(q)

    listing = client.get("/vods?$limit=50&$sort[createdAt]=-1").json()["data"]
    vod_ids = [v["id"] for v in listing]
    for vid in vod_ids[:5]:
        get(f"/vods/{vid}")
        get(f"/emotes?vod_id={vid}")
        get(f"/emotes/{vid}")

    # Chat: the vods with the most logs plus the newest one.
    for vid in ["2279668961", "2256405914", vod_ids[0]]:
        for off in COMMENT_OFFSETS:
            r = get(f"/v1/vods/{vid}/comments?content_offset_seconds={off}")
            cursor = r.json().get("cursor") if r.status_code == 200 else None
            for _ in range(CURSOR_PAGES if off == 0 else 1):
                if not cursor:
                    break
                r = get(f"/v1/vods/{vid}/comments?cursor={cursor}")
                cursor = r.json().get("cursor") if r.status_code == 200 else None
    get("/v1/vods/2279668961/comments")
    get("/v1/vods/2279668961/comments?cursor=not-base64")

    (HERE / "golden.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(out)} entries")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: capture_golden.py <legacy API base URL>")
    main(sys.argv[1])
