"""Home page SD form, option profiles and the prepare job."""

from __future__ import annotations

from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import domain_defaults
import services.image_catalog
import services.images
import services.sd_prepare
import services.storage


class SdCardTests(BaseCallbackSiteTest):
    def test_dashboard_setup_sections_open_one_at_a_time(self) -> None:
        self.browser_probe("/", [], "", """
const target = document.querySelector("#sd-target-settings");
const image = document.querySelector("#sd-image-settings");
const ssh = document.querySelector("#sd-ssh-settings");
check(target.open && !image.open && !ssh.open, "only target settings opens initially");
image.querySelector("summary").click();
check(image.open && !target.open && !ssh.open, "image settings closes target settings");
ssh.querySelector("summary").click();
check(ssh.open && !target.open && !image.open, "SSH access closes image settings");
target.querySelector("summary").click();
check(target.open && !image.open && !ssh.open, "target settings closes SSH access");
target.querySelector("summary").click();
check(!target.open && !image.open && !ssh.open, "all setup sections can be collapsed");
document.querySelector("#cloud-init-optional-settings").open = true;
image.querySelector("summary").click();
check(document.querySelector("#cloud-init-optional-settings").open,
  "cloud-init settings remains independent of the setup accordion");
""")

    def test_flash_helper_requires_current_device_identity(self) -> None:
        checks = self.browser_probe("/", ["home/state.js", "home/catalog_filters.js", "home/devices.js"], """
const initialSdOptionProfiles = {};
function updateDeviceActivity() {}
let confirmations = [];
window.confirm = message => { confirmations.push(message); return true; };
""", """
updateDeviceDiscoveryMode("helper");
const device = {
  path: "/dev/sdb", size: "32G", model: "USB Reader", serial: "reader-123",
  transport: "usb", identity: "device-v1"
};
replaceDeviceOptions([device]);
form.confirm_overwrite.checked = true;
const confirmation = confirmHelperTarget();
check(
  confirmation.device_identity === "device-v1"
    && confirmation.confirm_device === "/dev/sdb",
  "confirmed device identity is returned for the request"
);
check(confirmations[0].includes("Permanently erase")
  && confirmations[0].includes("Serial: reader-123"), "confirmation identifies the destructive target");
replaceDeviceOptions([{...device, identity: "device-v2"}]);
check(!form.confirm_overwrite.checked, "changed device identity clears overwrite consent");
check(deviceWebhookStatusEl.textContent.includes("identity changed"), "identity change is visible to the operator");
window.fetch = async () => ({
  ok: false,
  json: async () => ({
    backend: "helper", devices: [], errors: ["Flash helper is starting; discovery will retry."]
  })
});
const ready = await pollDevicesSnapshot();
check(!ready && form.device.value === "", "unavailable helper cannot leave a stale write target selected");
check(deviceWebhookStatusEl.textContent.includes("Flash helper is starting"),
  "helper startup errors are visible in the device status");
""")
        self.assertIn("changed device identity clears overwrite consent", checks)
    def test_setup_panels_take_turns_without_affecting_cloud_init_or_inputs(self) -> None:
        checks = self.browser_probe("/", [], "", """
const panels = [...document.querySelectorAll('#sd-form > details[name="sd-setup"]')];
const target = document.querySelector("#sd-target-settings");
const image = document.querySelector("#sd-image-settings");
const ssh = document.querySelector("#sd-ssh-settings");
const optional = document.querySelector("#cloud-init-optional-settings");
const openPanels = () => panels.filter(panel => panel.open);
check(panels.length === 3 && panels[0] === target && panels[1] === image && panels[2] === ssh,
  "three setup panels follow target, image, access order");
check(openPanels().length === 1 && target.open, "only Target Settings starts open");
document.querySelector("#hostname").value = "accordion-node";
target.querySelector("summary").focus();
image.querySelector("summary").click();
check(openPanels().length === 1 && image.open && !target.open, "image step closes target");
document.querySelector("#image_url").value = "https://example.invalid/image.img.xz";
ssh.querySelector("summary").click();
check(openPanels().length === 1 && ssh.open && !image.open, "access step closes image");
optional.open = true;
check(openPanels().length === 1 && ssh.open && optional.open, "Cloud-Init Settings remains independent");
target.querySelector("summary").click();
check(openPanels().length === 1 && target.open && !ssh.open, "earlier steps can be reopened");
check(document.querySelector("#hostname").value === "accordion-node"
  && document.querySelector("#image_url").value === "https://example.invalid/image.img.xz",
  "switching steps preserves input values");
image.open = true;
check(openPanels().length === 1 && image.open, "programmatic opening also stays exclusive");
image.querySelector("summary").click();
check(openPanels().length === 0 && optional.open, "active step can be closed without affecting Cloud-Init");
""")
        self.assertEqual(len(checks), 9)

    def test_misc_options_are_visible_and_map_to_provisioning(self) -> None:
        page = self.page_text(self.client.get("/"))
        self.assertIn('data-cloud-init-section="misc"', page)
        self.assertIn('data-option-key="configure_neovim"', page)
        self.assertIn('data-option-key="install_cli_tools"', page)
        env = {}
        enabled = services.sd_prepare._map_cloud_init_options_to_env(env, "standard", {
            "configure_neovim": True, "install_cli_tools": True,
            "packages": "jq, tmux, jq",
        })
        self.assertEqual(env["CLOUD_INIT_CONFIGURE_NEOVIM"], "true")
        self.assertEqual(env["CLOUD_INIT_INSTALL_CLI_TOOLS"], "true")
        self.assertEqual(env["CLOUD_INIT_EXTRA_PACKAGES"], "jq tmux")
        self.assertTrue({"configure_neovim", "install_cli_tools", "packages"}.issubset(enabled))
        services.sd_prepare._map_cloud_init_options_to_env(env, "standard", {
            "configure_neovim": False, "install_cli_tools": False,
        })
        self.assertEqual(env["CLOUD_INIT_CONFIGURE_NEOVIM"], "false")
        self.assertEqual(env["CLOUD_INIT_INSTALL_CLI_TOOLS"], "false")
        self.assertEqual(env["CLOUD_INIT_EXTRA_PACKAGES"], "")

    def test_misc_and_package_validation_prevents_starting_a_flash(self) -> None:
        for options in (
            {"packages": ["curl;reboot"]}, {"packages": ["--help"]},
            {"packages": ["git=1.0"]}, {"packages": [42]},
            {"packages": ["nvim"]},
            {"configure_neovim": "false"}, {"install_cli_tools": 1},
        ):
            with self.subTest(options=options):
                with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as thread:
                    response = self.client.post("/api/sd/prepare", json={
                        "profile": "pi5", "hostname": "misc-test", "device": "/dev/fake",
                        "ssh_public_key": "ssh-ed25519 test",
                        "callback_url": "http://example.invalid/callback",
                        "cloud_init_options": options,
                    })
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.get_json()["errors"])
                thread.assert_not_called()

    def test_misc_options_survive_saved_sd_templates(self) -> None:
        options = {"configure_neovim": True, "install_cli_tools": True, "packages": ["strace"]}
        response = self.client.post("/api/sd/options-profiles", json={
            "name": "misc-test", "distribution": "standard", "options": options,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        profiles = self.client.get("/api/sd/options-profiles").get_json()["profiles"]
        saved = next(profile for profile in profiles if profile["name"] == "misc-test")
        self.assertEqual(saved["options"], options)
        self.addCleanup(self.client.delete, "/api/sd/options-profiles/misc-test")

    def test_prepare_rejects_legacy_pi_os_urls_and_cached_files(self) -> None:
        for entry in domain_defaults.IMAGE_CATALOG:
            if entry.get("cloud_init_supported") is not False or not entry["image_url"]:
                continue
            cache_path = services.image_catalog._catalog_entry_with_availability(entry)["cache_path"]
            for source in ({"image_url": entry["image_url"]}, {"image_file": cache_path}):
                with self.subTest(image=entry["id"], source=source):
                    with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as thread:
                        response = self.client.post("/api/sd/prepare", json={
                            "profile": "pi3bplus", "hostname": "image-test", "device": "/dev/fake",
                            "ssh_public_key": "ssh-ed25519 test",
                            "callback_url": "http://example.invalid/callback", **source,
                        })
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("does not support cloud-init", " ".join(response.get_json()["errors"]))
                    thread.assert_not_called()

    def test_prepare_rejects_known_image_for_the_wrong_profile(self) -> None:
        entry = next(e for e in domain_defaults.IMAGE_CATALOG if e["id"] == "ubuntu-24.04-pi-arm64")
        payload = services.sd_prepare._sanitize_sd_prepare_payload({
            "profile": "network-toolkit", "image_url": entry["image_url"], "cloud_init_options": {},
        })
        self.assertIn("not compatible", " ".join(payload["config_errors"]))

    def test_presets_replace_stale_image_fields_and_use_selected_architecture(self) -> None:
        text = (domain_defaults.BASE_DIR / "static/home/catalog_filters.js").read_text()
        self.assertIn('const arch = selected?.dataset?.arch || profileArch(profile)', text)
        self.assertIn('form.image_sha256.value = imageSha;', text)
        self.assertNotIn('!form.image_sha256.value.trim()', text)
        self.assertIn('item.cloud_init_supported !== false', text)
        self.assertNotIn('The default Ubuntu 24.04 ARM64 image works', text)

    def test_prepare_job_uses_shared_catalog_cache_and_preserves_local_override(self) -> None:
        entry = next(e for e in domain_defaults.IMAGE_CATALOG if e["id"] ==
                     "network-toolkit-raspios-trixie-arm64-lite")
        for image_file in ("", "/tmp/manual.img"):
            with self.subTest(image_file=image_file):
                form = {"profile": "network-toolkit", "hostname": "cache-test", "device": "/dev/fake",
                        "ssh_public_key": "ssh-ed25519 test", "callback_url": "http://example.invalid/callback",
                        "image_file": image_file}
                job_id = "cache-env-test"
                services.sd_prepare.JOBS[job_id] = {"status": "queued"}
                with patch("os.geteuid", return_value=0), patch_in("_archive_ssh_public_key",
                    return_value={}), patch("services.sd_prepare.subprocess.Popen") as popen, patch_in("_record_image_history"):
                    popen.return_value.stdout = iter(())
                    popen.return_value.returncode = 0
                    services.sd_prepare._run_prepare_job(job_id, form)
                command = popen.call_args.args[0]
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env["IMAGE_CACHE_DIR"], str(services.storage.IMAGE_CACHE_DIR))
                if image_file:
                    self.assertNotIn("IMAGE_CACHE_PATH", env)
                    self.assertNotIn("--image-url", command)
                    self.assertEqual(command[command.index("--image-file") + 1], image_file)
                else:
                    self.assertEqual(command[command.index("--image-url") + 1], entry["image_url"])
                    self.assertEqual(command[command.index("--image-sha256") + 1], entry["image_sha256"])
                    self.assertEqual(env["IMAGE_CACHE_PATH"], str(services.storage.IMAGE_CACHE_DIR /
                        services.image_catalog._safe_catalog_filename(entry["image_url"], entry["id"])))
                services.sd_prepare.JOBS.pop(job_id)

    def test_home_page_contains_cloud_init_template_controls(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Cloud-Init Settings", text)
        self.assertIn("Profile Selection", text)
        self.assertIn("Save Current As Template", text)
        self.assertNotIn("Load Selected Template", text)
        self.assertNotIn('id="sd-options-load"', text)
        self.assertIn("Delete Selected Template", text)
        self.assertIn("Apply Security Starter Defaults", text)
        self.assertIn("Reset Optional Fields", text)
        self.assertIn("User Password (optional)", text)
        self.assertIn("Disable direct root SSH login", text)
        self.assertIn("Apply K3s security baseline (optional)", text)
        self.assertIn("Enable unattended upgrades (Debian/Ubuntu)", text)
        self.assertIn("Enable host firewall baseline (ufw/firewalld)", text)
        self.assertIn("Set SELinux to enforcing (RHEL-family)", text)
        self.assertIn("User-facing k3s template", text)
        self.assertIn("fields set", text)
        self.assertIn("fieldset-disclosure", text)
        self.assertIn("▾", text)
        self.assertNotIn("This operation erases the target device", text)

    def test_home_page_explains_required_k3s_fields(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Most users only need the server endpoint here; it tells K3s where the control-plane API lives.", text)
        self.assertIn("server: https://local-machine-ip:6443", text)

    def test_home_page_merges_pi_specific_templates_into_current_options(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn('const isPiOnlyProfile = Boolean(profile.pi_only);', text)
        self.assertIn('const currentOptions = gatherCloudInitOptions();', text)
        self.assertIn('applyCloudInitOptions(isPiOnlyProfile ? { ...currentOptions, ...profileOptions } : profileOptions);', text)
        self.assertIn("Loaded Pi-specific template '${name}' and merged it into the current optional settings.", text)

    def test_home_page_scopes_pi_boot_options_to_selected_pi_model(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn('id="pi_model_select"', text)
        self.assertEqual(text.count('data-pi-model="pi5"'), 3)
        self.assertIn('wrapperModel === piModel', text)

    def test_home_page_embeds_cached_catalog_metadata(self) -> None:
        entry = next(item for item in domain_defaults.IMAGE_CATALOG if str(item.get("image_url", "")).strip())
        cache_path = str(services.storage.IMAGE_CACHE_DIR / services.image_catalog._safe_catalog_filename(str(entry.get("image_url", "")), str(entry.get("id", ""))))
        services.images._append_image_history(
            {
                "recorded_at": "2026-07-01T00:00:00+00:00",
                "status": "ok",
                "source": "image_url",
                "source_value": entry.get("image_url", ""),
                "image_url": entry.get("image_url", ""),
                "download_path": cache_path,
                "expected_sha256": entry.get("image_sha256", ""),
                "observed_sha256": entry.get("image_sha256", ""),
            }
        )

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('"cache_path":', text)
        self.assertIn(str(entry.get("id", "")), text)
        self.assertIn(cache_path, text)

    def test_callback_rows_condense_older_events_per_host(self) -> None:
        from app import app as flask_app

        events = [
            {"hostname": "rpi5-01", "stage": "ready", "received_at": "2026-11-06T12:00:03+00:00"},
            {"hostname": "rpi5-02", "stage": "ready", "received_at": "2026-11-06T12:00:02+00:00"},
            {"hostname": "RPI5-01", "stage": "boot", "received_at": "2026-11-06T12:00:01+00:00"},
        ]
        with flask_app.test_request_context("/"):
            html = flask_app.jinja_env.get_template("fragments/callback_rows.html").render(events=events)
        self.assertEqual(html.count('class="event-row-condensed"'), 1)

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="events-toggle-older"', page)
        script = self.client.get("/static/home/events.js").get_data(as_text=True)
        self.assertIn("event-row-condensed", script)

    def test_home_page_loads_cloud_init_profile_on_select(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertNotIn("sdOptionsLoadBtn", text)
        self.assertIn(
            'sdOptionsProfileSelectEl.addEventListener("change", () => {\n'
            '  const name = String(sdOptionsProfileSelectEl.value || "").trim();\n'
            "  if (name && sdOptionProfiles[name]) {\n"
            "    loadSelectedSdOptionProfile();",
            text,
        )

    def test_home_page_motd_banner_has_collapsed_variables_pulldown(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="opt_motd_banner"', text)
        self.assertIn("Embeddable variables", text)
        self.assertIn('<details class="motd-vars-pulldown"', text)
        self.assertNotIn('<details class="motd-vars-pulldown" open', text)
        self.assertIn("${HOSTNAME}", text)
        self.assertIn("${PROFILE}", text)
        self.assertIn("${DISTRIBUTION}", text)
        self.assertIn("${CALLBACK_URL}", text)

    def test_home_page_contains_flash_mascot_hooks(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn("id=\"flash-mascot\"", text)
        self.assertIn("Happy flasher online", text)
        self.assertIn("flashMascotEl.classList.add(\"active\")", text)
        self.assertIn('SD_ACTIVE_JOB_KEY = "sd-active-job-id"', text)
        self.assertIn('SD_HOSTNAME_INCREMENTED_JOB_KEY = "sd-hostname-incremented-job-id"', text)
        self.assertIn("computeNextHostname", text)
        self.assertIn("maybeIncrementHostnameAfterSuccess", text)
        self.assertIn("resumeActiveJobIfAny", text)
        self.assertIn('href="/test-terminal"', text)

    def test_sd_option_profiles_crud(self) -> None:
        initial = self.client.get("/api/sd/options-profiles")
        self.assertEqual(initial.status_code, 200)
        self.assertTrue(initial.get_json()["ok"])

        bad = self.client.post(
            "/api/sd/options-profiles",
            json={"name": "", "options": {}},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.get_json()["ok"])

        create = self.client.post(
            "/api/sd/options-profiles",
            json={
                "name": "pi5-baseline",
                "description": "base bootstrap",
                "set_preferred": True,
                "pi_only": True,
                "options": {
                    "timezone": "UTC",
                    "packages": ["git", "curl"],
                    "disable_ssh_password_auth": True,
                },
            },
        )
        self.assertEqual(create.status_code, 200)
        create_payload = create.get_json()
        self.assertTrue(create_payload["ok"])
        self.assertEqual(create_payload["profile"]["name"], "pi5-baseline")
        self.assertTrue(create_payload["profile"].get("pi_only"))

        listed = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listed.status_code, 200)
        listed_payload = listed.get_json()
        names = [p.get("name") for p in listed_payload.get("profiles", [])]
        self.assertIn("pi5-baseline", names)
        self.assertEqual(names[0], "pi5-baseline")

        deleted = self.client.delete("/api/sd/options-profiles/pi5-baseline")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["ok"])

        missing_delete = self.client.delete("/api/sd/options-profiles/pi5-baseline")
        self.assertEqual(missing_delete.status_code, 404)
        self.assertFalse(missing_delete.get_json()["ok"])

    def test_sd_jobs_list_returns_active_jobs(self) -> None:
        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS.clear()
            services.sd_prepare.JOBS["job-queued"] = {
                "id": "job-queued",
                "status": "queued",
                "created_at": "2026-07-01T00:00:00+00:00",
                "request": {"hostname": "node-queued"},
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            services.sd_prepare.JOBS["job-running"] = {
                "id": "job-running",
                "status": "running",
                "created_at": "2026-07-01T00:00:30+00:00",
                "started_at": "2026-07-01T00:00:35+00:00",
                "request": {"hostname": "node-running"},
                "stdout": "Flashing image",
                "stderr": "",
                "exit_code": None,
            }
            services.sd_prepare.JOBS["job-done"] = {
                "id": "job-done",
                "status": "completed",
                "created_at": "2026-07-01T00:00:10+00:00",
                "finished_at": "2026-07-01T00:00:20+00:00",
                "request": {"hostname": "node-done"},
                "stdout": "done",
                "stderr": "",
                "exit_code": 0,
            }

        response = self.client.get("/api/sd/jobs")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        jobs = payload.get("jobs", [])
        job_ids = [job.get("id") for job in jobs]
        self.assertIn("job-running", job_ids)
        self.assertIn("job-queued", job_ids)
        self.assertNotIn("job-done", job_ids)

        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS.clear()

    def test_sd_prepare_auto_generates_sd_options_profile_when_missing(self) -> None:
        with patch_in("_ensure_prereqs", return_value=[]), patch("threading.Thread"):
            response = self.client.post(
                "/api/sd/prepare",
                json={
                    "profile": "pi5",
                    "hostname": "rack-node-auto",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA test",
                    "callback_url": "http://192.0.2.10:8088/api/cloud-init/callback",
                    "distribution": "k3s",
                    "pi_only": True,
                    "cloud_init_options": {
                        "timezone": "UTC",
                        "enable_fail2ban": True,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.sd_prepare.JOBS_LOCK:
            job = services.sd_prepare.JOBS.get(job_id)
            self.assertIsNotNone(job)
            request_payload = dict(job.get("request", {})) if job else {}

        generated_name = str(request_payload.get("sd_options_profile", ""))
        self.assertTrue(generated_name.startswith("generated-rack-node-auto-"))

        listing = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listing.status_code, 200)
        profiles = listing.get_json().get("profiles", [])
        self.assertTrue(profiles)
        self.assertEqual(profiles[0].get("name"), generated_name)
        self.assertTrue(profiles[0].get("pi_only"))

        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS.pop(job_id, None)

    def test_sanitize_sd_prepare_payload_includes_cloud_init_options(self) -> None:
        cleaned = services.sd_prepare._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-01",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "cloud_init_options": {"timezone": "UTC", "packages": ["git"]},
                "sd_options_profile": "base-template",
                "pi_only": True,
                "ignored": "value",
            }
        )
        self.assertNotIn("ignored", cleaned)
        self.assertEqual(cleaned["sd_options_profile"], "base-template")
        self.assertEqual(cleaned["cloud_init_options"]["timezone"], "UTC")
        self.assertTrue(cleaned["pi_only"])

    def test_sd_prepare_queues_cloud_init_options_metadata(self) -> None:
        with patch_in("_ensure_prereqs", return_value=[]), patch("threading.Thread") as thread_cls:
            response = self.client.post(
                "/api/sd/prepare",
                json={
                    "profile": "pi5",
                    "hostname": "rack-node-02",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA test",
                    "callback_url": "http://192.0.2.10:8088/api/cloud-init/callback",
                    "distribution": "k3s",
                    "sd_options_profile": "base-profile",
                    "cloud_init_options": {
                        "timezone": "UTC",
                        "packages": ["git", "curl"],
                        "k3s_config": "# pragma: k3s-config-v1\nserver: https://control-plane.example:6443\n",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.sd_prepare.JOBS_LOCK:
            job = services.sd_prepare.JOBS.get(job_id)
            self.assertIsNotNone(job)
            request_payload = dict(job.get("request", {})) if job else {}

        self.assertEqual(request_payload.get("sd_options_profile"), "base-profile")
        self.assertEqual(request_payload.get("distribution"), "k3s")
        self.assertEqual(request_payload.get("cloud_init_options", {}).get("timezone"), "UTC")
        self.assertEqual(request_payload.get("cloud_init_options", {}).get("packages"), ["git", "curl"])
        self.assertIn("pragma: k3s-config-v1", request_payload.get("cloud_init_options", {}).get("k3s_config", ""))

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))
        args_tuple = kwargs.get("args") or ()
        self.assertTrue(len(args_tuple) >= 2)
        self.assertEqual(args_tuple[0], job_id)

        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS.pop(job_id, None)

    def test_run_prepare_job_maps_security_cloud_init_options_to_env(self) -> None:
        class FakePopen:
            def __init__(self, *args, **kwargs) -> None:
                self.returncode = 0
                self.stdout = iter(())

            def wait(self) -> int:
                return self.returncode

        job_id = "job-security-env"
        form = {
            "profile": "pi5",
            "hostname": "rack-sec-01",
            "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAABAQC test",
            "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
            "distribution": "standard",
            "cloud_init_options": {
                "disable_ssh_password_auth": True,
                "disable_root_ssh": True,
                "enable_fail2ban": True,
                "enable_unattended_upgrades": True,
                "enable_power_restore_callback": True,
                "reboot_after_bootstrap": True,
                "usb_max_current_enable": True,
                "pciex1_enable": True,
                "pciex1_gen3": True,
                "ssh_port": "2222",
                "ssh_allow_groups": ["sudo", "wheel"],
                "extra_ssh_keys": [
                    "ssh-ed25519 AAAA111 extra1",
                    "ssh-ed25519 AAAA222 extra2",
                ],
            },
        }

        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS[job_id] = {
                "status": "queued",
                "created_at": "2026-01-01T00:00:00+00:00",
                "request": dict(form),
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }

        popen_call = {}

        def fake_popen(*args, **kwargs):
            popen_call.update(kwargs)
            return FakePopen()

        with patch("os.geteuid", return_value=0), patch_in("_archive_ssh_public_key", return_value={"archive_path": "test-archive"}
        ), patch("subprocess.Popen", side_effect=fake_popen), patch_in("_record_image_history"):
            services.sd_prepare._run_prepare_job(job_id, form)

        env = popen_call.get("env") or {}
        self.assertEqual(env.get("CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH"), "true")
        self.assertEqual(env.get("CLOUD_INIT_DISABLE_ROOT_SSH"), "true")
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_FAIL2BAN"), "true")
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES"), "true")
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK"), "true")
        self.assertEqual(env.get("CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP"), "true")
        self.assertEqual(env.get("CLOUD_INIT_USB_MAX_CURRENT_ENABLE"), "true")
        self.assertEqual(env.get("CLOUD_INIT_PCIEX1_ENABLE"), "true")
        self.assertEqual(env.get("CLOUD_INIT_PCIEX1_GEN3"), "true")
        self.assertEqual(env.get("CLOUD_INIT_SSH_PORT"), "2222")
        self.assertEqual(env.get("CLOUD_INIT_SSH_ALLOW_GROUPS"), "sudo,wheel")
        self.assertEqual(
            env.get("CLOUD_INIT_EXTRA_SSH_KEYS"),
            "ssh-ed25519 AAAA111 extra1\nssh-ed25519 AAAA222 extra2",
        )

        with services.sd_prepare.JOBS_LOCK:
            self.assertEqual(services.sd_prepare.JOBS[job_id]["status"], "completed")
            services.sd_prepare.JOBS.pop(job_id, None)

    def test_run_prepare_job_respects_disabled_power_restore_callback(self) -> None:
        class FakePopen:
            def __init__(self, *args, **kwargs) -> None:
                self.returncode = 0
                self.stdout = iter(())

            def wait(self) -> int:
                return self.returncode

        job_id = "job-power-restore-disabled"
        form = {
            "profile": "pi5",
            "hostname": "rack-sec-02",
            "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAABAQC test",
            "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
            "distribution": "standard",
            "cloud_init_options": {
                "enable_power_restore_callback": False,
            },
        }

        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS[job_id] = {
                "status": "queued",
                "created_at": "2026-01-01T00:00:00+00:00",
                "request": dict(form),
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }

        popen_call = {}

        def fake_popen(*args, **kwargs):
            popen_call.update(kwargs)
            return FakePopen()

        with patch("os.geteuid", return_value=0), patch_in("_archive_ssh_public_key", return_value={"archive_path": "test-archive"}
        ), patch("subprocess.Popen", side_effect=fake_popen), patch_in("_record_image_history"):
            services.sd_prepare._run_prepare_job(job_id, form)

        env = popen_call.get("env") or {}
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK"), "false")

        with services.sd_prepare.JOBS_LOCK:
            self.assertEqual(services.sd_prepare.JOBS[job_id]["status"], "completed")
            services.sd_prepare.JOBS.pop(job_id, None)

    def test_home_page_collapses_cloud_init_optional_settings_by_default(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<details class="fieldset" id="cloud-init-optional-settings">', text)
        self.assertIn("Settings relevant to the selected profile and distribution", text)
        self.assertNotIn('<details class="fieldset" open id="cloud-init-optional-settings">', text)

    def test_home_page_places_activity_dock_before_sd_form(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        dock_index = text.index("Operator Activity Dock 🐣✨")
        form_index = text.index("SD Card Imager 🧰")
        self.assertLess(dock_index, form_index)
