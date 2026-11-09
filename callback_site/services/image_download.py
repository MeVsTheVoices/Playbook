"""Bounded HTTP retries with validated byte-range resumption."""

from __future__ import annotations

import hashlib
from http.client import IncompleteRead
import logging
from pathlib import Path
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def download(url: str, target: Path, on_progress: Callable[[int, int | None], None] | None = None) -> str:
    offset = 0
    validator = ""
    digest = hashlib.sha256()
    total = None
    for attempt in range(5):
        headers = {"User-Agent": "callback-site-catalog-pull/1.0", "Accept-Encoding": "identity"}
        if offset and validator:
            headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
        else:
            offset = 0
            digest = hashlib.sha256()
        try:
            with urlopen(Request(url, headers=headers), timeout=120) as response:  # nosec B310
                status = response.status
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != offset or int(match[2]) != int(match[3]) - 1:
                        raise ValueError("Invalid image download Content-Range; refusing partial data.")
                    total = int(match[3])
                elif status == 200:
                    offset = 0
                    digest = hashlib.sha256()
                    length = response.headers.get("Content-Length")
                    total = int(length) if length is not None else None
                else:
                    raise ValueError(f"Unexpected image download HTTP status {status}.")
                etag = response.headers.get("ETag", "")
                validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified", "")
                with target.open("ab" if offset else "wb") as destination:
                    if on_progress:
                        on_progress(offset, total)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        destination.write(chunk)
                        digest.update(chunk)
                        offset += len(chunk)
                        if total is not None and offset > total:
                            raise ValueError("Image download exceeded its declared length.")
                        if on_progress:
                            on_progress(offset, total)
                if not offset or (total is not None and offset != total):
                    raise IncompleteRead(b"", (total or 1) - offset)
                return digest.hexdigest()
        except HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 4:
                raise
            logger.warning("Image download HTTP %s; retry %s/4.", exc.code, attempt + 1)
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
            if attempt == 4:
                raise
            logger.warning("Image download interrupted at %s bytes (%s); retry %s/4.",
                           offset, type(exc).__name__, attempt + 1)
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("Image download exhausted retries.")
