"""Image library, catalog and catalog pull jobs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import hashlib
import os
import subprocess
import tempfile
import shutil

try:
    from .test_support import BaseCallbackSiteTest, APP_DIR, TEST_DATA_DIR, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, APP_DIR, TEST_DATA_DIR, patch_in

import domain_defaults
import services.image_catalog
import services.storage


class ImageLibraryTests(BaseCallbackSiteTest):
    def test_missing_catalog_preview_is_named_bounded_and_matches_fragment(self) -> None:
        entries = [{"id": f"missing-{i}", "label": f"Missing image {i}", "profiles": ["pi5"], "image_url": f"https://example.invalid/{i}.img", "local_available": False} for i in range(8)]
        entries.append({"id": "cached", "label": "Cached image", "local_available": True})
        with patch("routes.images.IMAGE_CATALOG", entries), patch("routes.images._catalog_entry_with_availability", side_effect=lambda entry, **kwargs: entry):
            fragment = self.client.get("/fragments/sd/image-catalog-rows")
            page = self.client.get("/images")
        text = fragment.get_data(as_text=True)
        self.assertEqual(fragment.status_code, 200)
        self.assertEqual(text.count('class="compact-list-item"'), 5)
        self.assertIn("8 missing preset(s)", text)
        self.assertIn("Missing image 0", text)
        self.assertNotIn("Missing image 5", text)
        self.assertNotIn("Cached image", text)
        self.assertNotIn("<tr", text)
        self.assertIn(text.strip(), page.get_data(as_text=True))

    def test_missing_catalog_preview_reports_all_cached(self) -> None:
        with patch("routes.images.IMAGE_CATALOG", [{"id": "cached", "local_available": True}]), patch("routes.images._catalog_entry_with_availability", side_effect=lambda entry, **kwargs: entry):
            text = self.client.get("/fragments/sd/image-catalog-rows").get_data(as_text=True)
        self.assertIn("All catalog presets are available locally.", text)
        self.assertNotIn('class="compact-list-item"', text)
    def test_cli_rejects_legacy_pi_os_before_downloading_or_creating_workspace(self) -> None:
        script = APP_DIR.parent / "scripts/lib/image.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source in ("IMAGE_URL=https://downloads.raspberrypi.com/raspios_oldstable_lite_arm64/raspios-bookworm.img.xz",
                           "IMAGE_FILE=/tmp/raspios-bullseye.img"):
                with self.subTest(source=source):
                    result = subprocess.run(
                        ["bash", "-c", 'set -euo pipefail; PROFILE=pi3bplus; IMAGE_URL=""; IMAGE_FILE=""; '
                         f'{source}; WORKSPACE="$1/work"; SCRIPT_DIR="$3"; source "$SCRIPT_DIR/lib/common.sh"; source "$2"',
                         "test", tmp, str(script), str(APP_DIR.parent / "scripts")],
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("lack cloud-init", result.stderr)
                    self.assertFalse((root / "work").exists())

    def test_flash_downloads_once_under_concurrent_requests_and_preserves_cache(self) -> None:
        script = APP_DIR.parent / "scripts/lib/image.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "bin"
            tools.mkdir()
            payload = root / "payload.img"
            payload.write_bytes(b"test disk image")
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            curl = tools / "curl"
            curl.write_text(
                '#!/bin/sh\ncase "$1" in\n'
                '  -fsSLI) printf "Content-Length: 15\\n";;\n'
                '  -fsSL) echo download >> "$CALLS"; sleep 1; '
                '[ "${FAIL_DOWNLOAD:-0}" = 1 ] && exit 22; cp "$PAYLOAD" "$4";;\n'
                '  *) exit 22;;\nesac\n'
            )
            curl.chmod(0o755)
            env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                   "IMAGE_CACHE_DIR": str(root / "cache"), "PAYLOAD": str(payload),
                   "CALLS": str(root / "calls"), "DIGEST": digest}
            command = [
                "bash", "-c",
                'set -euo pipefail; PROFILE=pi5; IMAGE_FILE=""; IMAGE_URL=https://example.invalid/test.img; '
                'IMAGE_SHA256="$DIGEST"; WORKSPACE="$1/work"; SCRIPT_DIR="$2"; '
                'source "$2/lib/common.sh"; source "$2/lib/image.sh"; '
                'cmp "$IMG_TO_WRITE" "$PAYLOAD"',
                "test", str(root), str(APP_DIR.parent / "scripts"),
            ]
            processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True) for _ in range(2)]
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn("Cached image:", stdout)
            self.assertEqual((root / "calls").read_text().splitlines(), ["download"])
            cache = root / "cache" / services.image_catalog._safe_catalog_filename(
                "https://example.invalid/test.img", "unused")
            self.assertEqual(cache.read_bytes(), payload.read_bytes())
            self.assertEqual(list((root / "cache").glob("*.part")), [])
            result = subprocess.run(command, env={**env, "DIGEST": ""},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Image hash verified:", result.stdout)
            cache.write_bytes(b"corrupt")
            result = subprocess.run(command, env={**env, "DIGEST": ""},
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Image hash verification failed", result.stderr)
            self.assertEqual((root / "calls").read_text().splitlines(), ["download"])
            cache.unlink()
            result = subprocess.run(command, env={**env, "DIGEST": "0" * 64},
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(cache.exists())
            self.assertEqual(list((root / "cache").glob("*.part")), [])
            result = subprocess.run(command, env={**env, "FAIL_DOWNLOAD": "1"},
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(cache.exists())
            self.assertEqual(list((root / "cache").glob("*.part")), [])

    def test_cache_identity_is_url_based_and_legacy_files_are_reused(self) -> None:
        url = "https://example.invalid/shared.img.xz"
        self.assertEqual(services.image_catalog._safe_catalog_filename(url, "one"),
                         services.image_catalog._safe_catalog_filename(url, "two"))
        legacy = services.storage.IMAGE_CACHE_DIR / "legacy-shared.img.xz"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"existing")
        entry = services.image_catalog._catalog_entry_with_availability(
            {"id": "legacy", "image_url": url})
        self.assertTrue(entry["local_available"])
        self.assertEqual(entry["cache_path"], str(legacy))

    def test_dashboard_shows_actual_cache_date_with_localized_formatting(self) -> None:
        cache = services.storage.IMAGE_CACHE_DIR / "date-test.img"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"image")
        os.utime(cache, (1794096000, 1794096000))
        from services.images import _append_image_history
        _append_image_history({"source_value": "https://example.invalid/date-test.img",
                               "download_path": str(cache), "recorded_at": "2026-11-08T03:00:00Z"})
        text = self.client.get("/").get_data(as_text=True)
        self.assertIn('Cached on <time id="image-cached-at"', text)
        self.assertIn('datetime="2026-11-08T00:00:00+00:00"', text)
        script = self.client.get("/static/home/init.js").get_data(as_text=True)
        self.assertIn('dateStyle: "long", timeStyle: "short"', script)
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for localized date rendering validation")
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "date.html"
            page.write_text(
                '<time id="image-cached-at" datetime="2026-11-08T00:00:00+00:00"></time>'
                f'<script>{script.split("toggleBtn.addEventListener", 1)[0]}</script>'
            )
            result = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--lang=en-US",
                 f"--user-data-dir={tmp}/chrome", "--dump-dom", page.as_uri()],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r">November (7|8), 2026 at ")

    def test_catalog_checksum_failure_does_not_replace_valid_cached_image(self) -> None:
        entry = {"id": "hash-failure", "image_url": "https://example.invalid/hash.img",
                 "image_sha256": "0" * 64}
        target = services.storage.IMAGE_CACHE_DIR / services.image_catalog._safe_catalog_filename(
            entry["image_url"], entry["id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original")
        job_id = "hash-failure"
        services.image_catalog.CATALOG_PULL_JOBS[job_id] = {"status": "queued", "results": []}
        def download(url, path, on_progress=None):
            path.write_bytes(b"wrong")
            return hashlib.sha256(b"wrong").hexdigest()
        with patch_in("_download_catalog_image", side_effect=download):
            services.image_catalog._run_catalog_pull_job(job_id, [entry], skip_existing=False)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(services.image_catalog.CATALOG_PULL_JOBS[job_id]["summary"]["failed"], 1)
        self.assertEqual(list(target.parent.glob(f"{target.name}*.part")), [])
        entry["image_sha256"] = hashlib.sha256(b"original").hexdigest()
        services.image_catalog.CATALOG_PULL_JOBS[job_id] = {"status": "queued", "results": []}
        with patch_in("_download_catalog_image") as downloader:
            services.image_catalog._run_catalog_pull_job(job_id, [entry], skip_existing=True)
            downloader.assert_not_called()
        self.assertEqual(services.image_catalog.CATALOG_PULL_JOBS[job_id]["summary"]["skipped"], 1)

    def test_dashboard_cache_date_is_available_without_image_history(self) -> None:
        entry = domain_defaults.IMAGE_CATALOG[0]
        target = services.storage.IMAGE_CACHE_DIR / services.image_catalog._safe_catalog_filename(
            entry["image_url"], entry["id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"cached by CLI")
        os.utime(target, (1794096000, 1794096000))
        with patch_in("_read_image_history", return_value=[]):
            summary = services.image_catalog._image_state_summary()
        self.assertTrue(summary["latest_available"])
        self.assertEqual(summary["latest_cached_at"], "2026-11-08T00:00:00+00:00")
        self.assertEqual(summary["latest_cache_path"], str(target))

    def test_images_page_exposes_grouped_catalog_and_manual_record_controls(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Add image manually", text)
        self.assertIn("Pull", text)
        self.assertIn('id="managed-images-body"', text)
        self.assertIn('id="image-catalog-body"', text)
        self.assertIn('compact-list', text)
        self.assertIn('images.css', text)
        self.assertIn('images.js', text)
        self.assertIn('window.__IMAGE_CATALOG__', text)

    def test_images_page_has_bulk_pull_controls(self) -> None:
        text = self.client.get("/images").get_data(as_text=True)
        self.assertIn('id="pull-all-missing"', text)
        script = self.client.get("/static/images.js").get_data(as_text=True)
        self.assertIn("catalog-select-all", script)
        self.assertIn("Pull selected", script)
    def test_images_page_renders_collapsible_manual_section(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('class="collapsible-summary"', text)
        self.assertIn('class="collapsible-indicator"', text)
        self.assertNotIn('id="retrieved-image-manager" open', text)

    def test_images_page_includes_pull_progress_markup(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="catalog-pull-progress-wrap"', text)
        self.assertIn('id="catalog-pull-progress-fill"', text)
        self.assertIn('🙂', text)

    def test_images_pull_progress_copy_is_concise(self) -> None:
        text = (APP_DIR / "static" / "images.js").read_text(encoding="utf-8")
        self.assertIn("Download in progress.", text)
        self.assertIn("Download complete.", text)
        self.assertIn("Download failed.", text)
        self.assertNotIn("Still working. The bar is moving and the tiles are reacting.", text)

    def test_images_catalog_group_defaults_keep_missing_collapsed(self) -> None:
        text = (APP_DIR / "static" / "images.js").read_text(encoding="utf-8")
        self.assertIn('title: "Missing from cache"', text)
        self.assertIn('open: false', text)
        self.assertIn('title: "Already downloaded"', text)

    def test_catalog_availability_endpoint(self) -> None:
        response = self.client.get("/api/sd/catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        entries = payload.get("entries") or []
        self.assertTrue(entries)
        first = entries[0]
        self.assertIn("local_available", first)
        self.assertIn("cache_path", first)

    def test_image_catalog_endpoint_uses_yaml_source_of_truth(self) -> None:
        response = self.client.get("/api/sd/image-catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        ids = {str(entry.get("id", "")) for entry in (payload.get("entries") or [])}
        self.assertIn("k3s-ubuntu-24.04-pi-arm64", ids)
        self.assertIn("ubuntu-24.04-pi-arm64", ids)

    def test_k3s_catalog_entry_is_presented_as_k3s_only(self) -> None:
        response = self.client.get("/api/sd/image-catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        entries = payload.get("entries") or []
        k3s_entry = next((entry for entry in entries if entry.get("id") == "k3s-ubuntu-24.04-pi-arm64"), None)
        self.assertIsNotNone(k3s_entry)
        self.assertEqual(str(k3s_entry.get("label", "")), "K3s")
        self.assertEqual(str(k3s_entry.get("distro", "")), "K3s")

    def test_image_state_summary_prefers_recent_available_state_entry(self) -> None:
        history = [
            {
                "recorded_at": "2026-07-10T20:00:00+00:00",
                "label": "newest-no-path",
                "job_status": "completed",
                "local_available": False,
                "download_path": "",
                "cache_path": "",
            },
            {
                "recorded_at": "2026-07-10T19:59:00+00:00",
                "label": "older-with-cache",
                "job_status": "completed",
                "local_available": True,
                "download_path": "/tmp/cache/image.img.xz",
            },
        ]

        with patch_in("_read_image_history", return_value=history), patch_in("_available_image_urls_from_history",
            return_value=set(),
        ), patch_in("_catalog_entry_with_availability",
            return_value={"local_available": False},
        ):
            summary = services.image_catalog._image_state_summary()

        self.assertEqual(summary.get("latest_label"), "newest-no-path")
        self.assertTrue(summary.get("latest_available"))
        self.assertEqual(summary.get("latest_cache_path"), "/tmp/cache/image.img.xz")
        self.assertEqual(summary.get("latest_state_emoji"), "🎉")

    def test_image_catalog_is_yaml_backed_and_has_intended_urls(self) -> None:
        entries = {entry.get("id"): entry for entry in domain_defaults.IMAGE_CATALOG}
        expected_files = {
            "raspios-trixie-arm64": "2026-06-18-raspios-trixie-arm64.img.xz",
            "raspios-trixie-armhf": "2026-06-18-raspios-trixie-armhf.img.xz",
            "raspios-bookworm-arm64-lite": "2026-06-18-raspios-bookworm-arm64-lite.img.xz",
            "raspios-bookworm-armhf-lite": "2026-06-18-raspios-bookworm-armhf-lite.img.xz",
            "ubuntu-24.04-pi-arm64": "ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
            "ubuntu-22.04-pi-arm64": "ubuntu-22.04.4-preinstalled-server-arm64+raspi.img.xz",
            "k3s-ubuntu-24.04-pi-arm64": "ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
        }

        for entry_id, expected_suffix in expected_files.items():
            self.assertIn(entry_id, entries)
            entry = entries[entry_id]
            self.assertTrue(entry.get("image_url"), msg=f"missing image_url for {entry_id}")
            self.assertTrue(entry["image_url"].endswith(expected_suffix))
            self.assertEqual(
                services.image_catalog._safe_catalog_filename(entry["image_url"], entry_id),
                hashlib.sha256(entry["image_url"].encode()).hexdigest() + ".img.xz",
            )

    def test_images_page_has_cache_path_color_hooks(self) -> None:
        with self.client.get("/images") as response:
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
        with self.client.get("/static/images.js") as assets:
            self.assertEqual(assets.status_code, 200)
            script = assets.get_data(as_text=True)
        self.assertIn("cache-path-ok", script)
        self.assertIn("cache-path-missing", script)
        self.assertIn("source-panel", text)
        self.assertIn("Distro image presets", text)
        self.assertIn("download-state available", script)
        self.assertIn("download-state missing", script)

    def test_image_crud_and_fragment_rows(self) -> None:
        created = self.client.post(
            "/api/sd/images",
            json={
                "label": "Ubuntu 24.04 LTS",
                "profile": "pi5",
                "source": "image_url",
                "source_value": "https://example.invalid/image.img.xz",
                "download_path": "/tmp/image.img.xz",
                "hash_status": "verified",
                "expected_sha256": "abc123",
                "observed_sha256": "abc123",
                "job_status": "completed",
                "exit_code": 0,
                "notes": "seed record",
            },
        )
        self.assertEqual(created.status_code, 201)
        created_payload = created.get_json()
        self.assertTrue(created_payload["ok"])
        record_id = created_payload["record"]["id"]

        listing = self.client.get("/api/sd/images")
        self.assertEqual(listing.status_code, 200)
        listed_entries = listing.get_json()["entries"]
        self.assertTrue(any(entry["id"] == record_id for entry in listed_entries))

        updated = self.client.put(
            f"/api/sd/images/{record_id}",
            json={"label": "Ubuntu 24.04 LTS Updated", "notes": "edited record"},
        )
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json()
        self.assertEqual(updated_payload["record"]["label"], "Ubuntu 24.04 LTS Updated")
        self.assertEqual(updated_payload["record"]["notes"], "edited record")

        rows = self.client.get("/fragments/sd/images-rows")
        self.assertEqual(rows.status_code, 200)
        self.assertIn("Ubuntu 24.04 LTS Updated", rows.get_data(as_text=True))

        deleted = self.client.delete(f"/api/sd/images/{record_id}")
        self.assertEqual(deleted.status_code, 200)
        after_delete = self.client.get("/api/sd/images")
        self.assertFalse(any(entry["id"] == record_id for entry in after_delete.get_json()["entries"]))

    def test_image_relative_download_path_availability(self) -> None:
        rel_dir = TEST_DATA_DIR / "cache" / "images"
        rel_dir.mkdir(parents=True, exist_ok=True)
        rel_file = rel_dir / "example.img.xz"
        rel_file.write_text("dummy", encoding="utf-8")

        created = self.client.post(
            "/api/sd/images",
            json={
                "label": "Relative Path Image",
                "profile": "pi5",
                "source": "catalog_pull",
                "source_value": "https://example.invalid/example.img.xz",
                "download_path": "cache/images/example.img.xz",
                "hash_status": "verified",
                "job_status": "completed",
                "exit_code": 0,
            },
        )
        self.assertEqual(created.status_code, 201)
        record_id = created.get_json()["record"]["id"]

        listing = self.client.get("/api/sd/images")
        self.assertEqual(listing.status_code, 200)
        rows = listing.get_json().get("entries", [])
        match = [row for row in rows if row.get("id") == record_id]
        self.assertTrue(match)
        self.assertTrue(match[0].get("local_available"))

    def test_catalog_pull_api_validation(self) -> None:
        empty = self.client.post("/api/sd/catalog-pull", json={"catalog_ids": []})
        self.assertEqual(empty.status_code, 400)
        self.assertFalse(empty.get_json()["ok"])

        no_url = self.client.post("/api/sd/catalog-pull", json={"catalog_ids": ["custom-arm64"]})
        self.assertEqual(no_url.status_code, 400)
        no_url_payload = no_url.get_json()
        self.assertFalse(no_url_payload["ok"])
        self.assertIn("custom-arm64", no_url_payload.get("missing_url_ids", []))

        not_found = self.client.get("/api/sd/catalog-pull/jobs/does-not-exist")
        self.assertEqual(not_found.status_code, 404)
        self.assertFalse(not_found.get_json()["ok"])

    def test_catalog_pull_worker_completes_and_records_progress(self) -> None:
        job_id = "job-test-catalog"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }

        def fake_download(url: str, target: Path, on_progress=None) -> str:
            if callable(on_progress):
                on_progress(4, 8)
            target.write_text("payload", encoding="utf-8")
            return "abc123"

        with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
            services.image_catalog.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        with patch_in("_download_catalog_image", side_effect=fake_download), patch_in("_append_image_history",
            return_value=None,
        ):
            services.image_catalog._run_catalog_pull_job(job_id, [entry], skip_existing=False)

        with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
            job = services.image_catalog.CATALOG_PULL_JOBS[job_id]

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["summary"]["ok"], 1)
        self.assertEqual(job["summary"]["running"], 0)
        self.assertEqual(job["summary"]["total"], 1)
        self.assertEqual(job["results"][0]["status"], "ok")

    def test_catalog_pull_worker_skips_stale_unwritable_part_file(self) -> None:
        job_id = "job-test-catalog-stale-part"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }
        target = services.storage.IMAGE_CACHE_DIR / services.image_catalog._safe_catalog_filename(str(entry["image_url"]), str(entry["id"]))
        stale_tmp = target.with_suffix(target.suffix + ".part")
        stale_tmp.parent.mkdir(parents=True, exist_ok=True)
        stale_tmp.write_text("stale", encoding="utf-8")
        stale_tmp.chmod(0o444)

        def fake_download(url: str, target_path: Path, on_progress=None) -> str:
            if callable(on_progress):
                on_progress(4, 8)
            target_path.write_text("payload", encoding="utf-8")
            return "abc123"

        with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
            services.image_catalog.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        try:
            with patch_in("_download_catalog_image", side_effect=fake_download), patch_in("_append_image_history",
                return_value=None,
            ):
                services.image_catalog._run_catalog_pull_job(job_id, [entry], skip_existing=False)

            with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
                job = services.image_catalog.CATALOG_PULL_JOBS[job_id]

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["summary"]["ok"], 1)
            self.assertEqual(job["results"][0]["status"], "ok")
            self.assertTrue(target.exists())
        finally:
            if target.exists():
                target.unlink()
            if stale_tmp.exists():
                stale_tmp.chmod(0o644)
                stale_tmp.unlink()

    def test_catalog_pull_worker_does_not_remove_other_jobs_partial_files(self) -> None:
        job_id = "job-test-catalog-stale-part-cleanup"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }
        target = services.storage.IMAGE_CACHE_DIR / services.image_catalog._safe_catalog_filename(str(entry["image_url"]), str(entry["id"]))
        stale_tmp = target.with_suffix(target.suffix + ".part")
        stale_tmp.parent.mkdir(parents=True, exist_ok=True)
        stale_tmp.write_text("stale", encoding="utf-8")

        def fake_download(url: str, target_path: Path, on_progress=None) -> str:
            if callable(on_progress):
                on_progress(4, 8)
            target_path.write_text("payload", encoding="utf-8")
            return "abc123"

        with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
            services.image_catalog.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        try:
            with patch_in("_download_catalog_image", side_effect=fake_download), patch_in("_append_image_history",
                return_value=None,
            ):
                services.image_catalog._run_catalog_pull_job(job_id, [entry], skip_existing=False)

            self.assertTrue(stale_tmp.exists())
            self.assertTrue(target.exists())
        finally:
            if target.exists():
                target.unlink()
            if stale_tmp.exists():
                stale_tmp.unlink()

    def test_catalog_pull_worker_initializes_running_summary_before_download(self) -> None:
        job_id = "job-test-catalog-running-summary"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }
        observed_summary: dict[str, int] = {}

        def fake_download(url: str, target_path: Path, on_progress=None) -> str:
            with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
                job = services.image_catalog.CATALOG_PULL_JOBS[job_id]
                observed_summary.update(job.get("summary", {}))
            if callable(on_progress):
                on_progress(4, 8)
            target_path.write_text("payload", encoding="utf-8")
            return "abc123"

        with services.image_catalog.CATALOG_PULL_JOBS_LOCK:
            services.image_catalog.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        try:
            with patch_in("_download_catalog_image", side_effect=fake_download), patch_in("_append_image_history",
                return_value=None,
            ):
                services.image_catalog._run_catalog_pull_job(job_id, [entry], skip_existing=False)

            self.assertEqual(observed_summary.get("running"), 1)
            self.assertEqual(observed_summary.get("total"), 1)
        finally:
            target = services.storage.IMAGE_CACHE_DIR / services.image_catalog._safe_catalog_filename(str(entry["image_url"]), str(entry["id"]))
            if target.exists():
                target.unlink()
