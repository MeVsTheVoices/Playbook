"""Image library, catalog and catalog pull jobs."""

from __future__ import annotations

from pathlib import Path

try:
    from .test_support import BaseCallbackSiteTest, APP_DIR, TEST_DATA_DIR, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, APP_DIR, TEST_DATA_DIR, patch_in

import domain_defaults
import services.image_catalog
import services.storage


class ImageLibraryTests(BaseCallbackSiteTest):
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
                f"{entry_id}-{expected_suffix}",
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

    def test_catalog_pull_worker_removes_stale_part_files_before_download(self) -> None:
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

            self.assertFalse(stale_tmp.exists())
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
