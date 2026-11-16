"""Wi-Fi validation and non-destructive seed generation."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml

from domain_defaults import IMAGE_CATALOG
from services.sd_prepare import _map_cloud_init_options_to_env, _sanitize_sd_prepare_payload, _wifi_image_renderer
from wifi_networks import combined_networks, network_config, parse_networks

ROOT = Path(__file__).resolve().parents[2]
NETWORKS = [
    {"ssid": "backup network", "password": "backup-password"},
    {"ssid": "mobile", "password": "mobile-password"},
]


class WifiNetworkTests(unittest.TestCase):
    def test_primary_and_additional_networks_preserve_values(self):
        options = {"wifi_ssid": " home ", "wifi_psk": " passphrase ", "wifi_networks": json.dumps(NETWORKS)}
        networks = combined_networks(options)
        self.assertEqual(networks[0], {"ssid": " home ", "password": " passphrase "})
        config = network_config(networks, "GB", renderer="networkd", interface="wlp2s0")
        self.assertEqual(config["wifis"]["wlp2s0"]["access-points"]["backup network"]["password"], "backup-password")
        self.assertTrue(config["ethernets"]["ethernet"]["dhcp4"])
        self.assertTrue(config["ethernets"]["ethernet"]["optional"])
        self.assertEqual(config["wifis"]["wlp2s0"]["regulatory-domain"], "GB")
        env = {}
        _map_cloud_init_options_to_env(env, "standard", options)
        self.assertEqual(json.loads(env["CLOUD_INIT_WIFI_NETWORKS"]), NETWORKS)

    def test_invalid_networks_and_duplicates_are_rejected_without_secrets_in_error(self):
        invalid = [
            "not json", "{}", json.dumps([{"ssid": "short", "password": "secret"}]),
            json.dumps([{"ssid": "bad\nname", "password": "long-password"}]),
            json.dumps([{"ssid": "x" * 33, "password": "long-password"}]),
            json.dumps([NETWORKS[0], NETWORKS[0]]),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                parse_networks(value)
            self.assertNotIn("backup-password", str(error.exception))
        with self.assertRaises(ValueError):
            combined_networks({"wifi_ssid": NETWORKS[0]["ssid"], "wifi_psk": "primary-password", "wifi_networks": json.dumps(NETWORKS)})
        self.assertEqual(parse_networks(json.dumps([{"ssid": "hex", "password": "a" * 64}]))[0]["password"], "a" * 64)

    def test_image_selection_and_invalid_interface_fail_before_job(self):
        options = {"wifi_ssid": "home", "wifi_psk": "good-password", "wifi_country": "US"}
        self.assertEqual(_wifi_image_renderer({"profile": "pi5"}), "networkd")
        self.assertEqual(_wifi_image_renderer({"profile": "playbook-server"}), "networkd")
        entry = next(item for item in IMAGE_CATALOG if item["id"] == "raspios-trixie-arm64")
        self.assertEqual(_wifi_image_renderer({"profile": "pi5", "image_url": entry["image_url"]}), "NetworkManager")
        with patch("services.sd_prepare._catalog_entry_with_availability",
                   side_effect=lambda item: {"cache_path": f"/cache/{item['id']}.img"}):
            self.assertEqual(_wifi_image_renderer({"profile": "pi5", "image_file": "/cache/raspios-trixie-arm64.img"}), "NetworkManager")
        custom = _sanitize_sd_prepare_payload({"profile": "pi5", "image_url": "https://example.invalid/custom.img", "cloud_init_options": options.copy()})
        self.assertIn("catalog image", " ".join(custom["config_errors"]))
        bad = _sanitize_sd_prepare_payload({"profile": "pi5", "cloud_init_options": {**options, "wifi_interface": "wlan0;bad"}})
        self.assertIn("interface", " ".join(bad["config_errors"]))
        no_country = _sanitize_sd_prepare_payload({"profile": "pi5", "cloud_init_options": {"wifi_networks": json.dumps(NETWORKS)}})
        self.assertIn("country", " ".join(no_country["config_errors"]))
        self.assertNotIn("config_errors", _sanitize_sd_prepare_payload({"profile": "pi5", "cloud_init_options": options.copy()}))

    def test_cli_rejects_bad_wifi_before_device_access(self):
        env = {**os.environ, "CLOUD_INIT_WIFI_SSID": "home", "CLOUD_INIT_WIFI_PSK": "short",
               "CLOUD_INIT_WIFI_COUNTRY": "US", "CLOUD_INIT_K3S_SERVER_HOST": "127.0.0.1"}
        result = subprocess.run([
            "bash", str(ROOT / "scripts" / "prepare_sd_cloud_init.sh"), "--profile", "pi5",
            "--hostname", "test-node", "--device", "/dev/not-real", "--ssh-key-file", "/does-not-exist",
            "--callback-url", "http://example.invalid/callback",
        ], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Wi-Fi passwords", result.stderr)
        self.assertNotIn("Device does not exist", result.stderr)

    def test_validate_generates_network_config_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            script_dir = ROOT / "scripts"
            env = {
                **os.environ, "SCRIPT_DIR": str(script_dir), "TMP_DIR": tmp,
                "META_DATA_OUT": str(path / "meta-data"), "INSTANCE_ID": "instance",
                "HOSTNAME": "node", "VALIDATE_CLOUD_INIT_SCHEMA": "false",
                "CLOUD_INIT_WIFI_SSID": "home", "CLOUD_INIT_WIFI_PSK": "primary-password",
                "CLOUD_INIT_WIFI_NETWORKS": json.dumps(NETWORKS), "CLOUD_INIT_WIFI_COUNTRY": "US",
                "CLOUD_INIT_WIFI_RENDERER": "networkd", "CLOUD_INIT_WIFI_INTERFACE": "wlp2s0",
            }
            for profile in ("pi5", "playbook-server"):
                result = subprocess.run(
                    ["bash", "-c", 'set -eu; source "$SCRIPT_DIR/lib/common.sh"; source "$SCRIPT_DIR/lib/validate.sh"'],
                    env={**env, "PROFILE": profile}, text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                config = yaml.safe_load((path / "network-config").read_text())
                self.assertEqual(set(config["wifis"]["wlp2s0"]["access-points"]), {"home", "backup network", "mobile"})
                self.assertEqual(config["ethernets"]["ethernet"]["optional"], profile != "playbook-server")
            result = subprocess.run(
                ["bash", "-c", 'set -eu; source "$SCRIPT_DIR/lib/common.sh"; source "$SCRIPT_DIR/lib/validate.sh"'],
                env={**env, "PROFILE": "pi5", "CLOUD_INIT_WIFI_NETWORKS": "bad"},
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("backup-password", result.stderr)

    def test_boot_seed_is_copied_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "boot" / "boot" / "firmware").mkdir(parents=True)
            for name in ("user-data", "meta-data", "network-config"):
                (path / name).write_text(name, encoding="utf-8")
            env = {**os.environ, "SCRIPT_DIR": str(ROOT / "scripts"), "TMP_DIR": tmp,
                   "USER_DATA_OUT": str(path / "user-data"), "META_DATA_OUT": str(path / "meta-data"),
                   "PROFILE": "pi5", "DISTRIBUTION": "standard", "DEVICE": "/dev/fake",
                   "BOOT_PART": "/dev/fake1", "HOSTNAME": "node"}
            body = ('set -eu; sync() { :; }; umount() { :; }; blockdev() { :; }; '
                    'mount() { if [[ "${CORRUPT:-}" == yes ]]; then printf bad > "$TMP_DIR/boot/network-config"; fi; }; '
                    'source "$SCRIPT_DIR/lib/finish.sh"')
            result = subprocess.run(["bash", "-c", body], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for directory in (path / "boot", path / "boot" / "boot" / "firmware"):
                self.assertEqual((directory / "network-config").read_text(), "network-config")
            result = subprocess.run(["bash", "-c", body], env={**env, "CORRUPT": "yes"}, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("readback failed", result.stderr)

    def test_server_seed_is_copied_privately_and_verified(self):
        source = (ROOT / "scripts" / "lib" / "server_image.sh").read_text()
        start = source.index('install -d -m 0755 "$TMP_DIR/root$SEED_DIR"')
        end = source.index('umount "$TMP_DIR/root"', source.index('for seed_file in "${seed_files[@]}"')) + len('umount "$TMP_DIR/root"')
        fragment = source[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name in ("user-data", "meta-data", "network-config"):
                (path / name).write_text(name, encoding="utf-8")
            env = {**os.environ, "TMP_DIR": tmp, "SEED_DIR": "/seed",
                   "USER_DATA_OUT": str(path / "user-data"), "META_DATA_OUT": str(path / "meta-data")}
            prefix = ('set -eu; sync() { :; }; umount() { :; }; '
                      'mount() { if [[ "${CORRUPT:-}" == yes ]]; then printf bad > "$TMP_DIR/root$SEED_DIR/network-config"; fi; }; '
                      'ROOT_PART=/dev/fake; ')
            result = subprocess.run(["bash", "-c", prefix + fragment], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            seed = path / "root" / "seed" / "network-config"
            self.assertEqual(seed.read_text(), "network-config")
            self.assertEqual(seed.stat().st_mode & 0o777, 0o600)
            result = subprocess.run(["bash", "-c", prefix + fragment], env={**env, "CORRUPT": "yes"}, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("readback failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
