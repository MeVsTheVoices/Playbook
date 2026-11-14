"""Transfer integrity and resumable reads without accessing external servers."""

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import test_support
except ImportError:
    import test_support

from services.image_download import download


class Response(io.BytesIO):
    def __init__(self, content, status=200, headers=None, fail=False):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}
        self.fail = fail

    def read(self, size=-1):
        if self.fail and self.tell():
            raise TimeoutError("simulated stall")
        return super().read(size)


class ImageDownloadTests(unittest.TestCase):
    def run_download(self, responses):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("services.image_download.urlopen", side_effect=responses) as open_url, \
             patch("services.image_download.time.sleep"):
            path = Path(tmp) / "download"
            digest = download("https://example.invalid/image", path)
            return path.read_bytes(), digest, open_url.call_args_list

    def test_timeout_resumes_with_if_range_and_validates_whole_digest(self):
        first = Response(b"first", headers={"Content-Length": "11", "ETag": '"v1"'}, fail=True)
        rest = Response(b"second", status=206, headers={"Content-Range": "bytes 5-10/11", "ETag": '"v1"'})
        contents, digest, calls = self.run_download([first, rest])
        self.assertEqual(contents, b"firstsecond")
        self.assertEqual(digest, hashlib.sha256(contents).hexdigest())
        self.assertEqual(calls[1].args[0].get_header("Range"), "bytes=5-")
        self.assertEqual(calls[1].args[0].get_header("If-range"), '"v1"')

    def test_ignored_range_restarts_without_duplicate_bytes(self):
        first = Response(b"bad", headers={"Content-Length": "6", "ETag": '"v1"'}, fail=True)
        contents, digest, _ = self.run_download([first, Response(b"correct", headers={"Content-Length": "7"})])
        self.assertEqual(contents, b"correct")
        self.assertEqual(digest, hashlib.sha256(contents).hexdigest())

    def test_truncated_body_retries_but_never_returns_success(self):
        responses = [Response(b"short", headers={"Content-Length": "10"}) for _ in range(5)]
        from http.client import IncompleteRead
        with self.assertRaises(IncompleteRead):
            self.run_download(responses)

    def test_invalid_range_is_rejected(self):
        first = Response(b"first", headers={"Content-Length": "11", "ETag": '"v1"'}, fail=True)
        rest = Response(b"second", status=206, headers={"Content-Range": "bytes 0-5/11"})
        with self.assertRaisesRegex(ValueError, "Content-Range"):
            self.run_download([first, rest])


if __name__ == "__main__":
    unittest.main()
