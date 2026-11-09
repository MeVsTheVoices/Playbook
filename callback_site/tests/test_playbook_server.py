"""Self-hosting profile and integration with both flashing backends."""

import unittest

import playbook_server as server
from domain_defaults import IMAGE_CATALOG, NODE_PROVISION_PROFILES
from services.live_config import readable_ids
from services.node_state import _default_profiles_for_group
from services.sd_prepare import _map_cloud_init_options_to_env, _sanitize_sd_prepare_payload


class PlaybookServerTests(unittest.TestCase):
    def test_defaults_render_the_activation_files(self):
        self.assertEqual(server.validate_options({}), [])
        files = {entry["path"]: entry for entry in server.render_files({})}
        self.assertIn("PLAYBOOK_PORT=8088", files["/etc/playbook-server.env"]["content"])
        self.assertEqual(files["/usr/local/bin/playbook-server-activate"]["permissions"], "0755")
        self.assertIn("playbook-server-activate", files["/usr/local/bin/playbook-update"]["content"])

    def test_rejects_invalid_settings(self):
        for options in (
            {"server_repo_url": "file:///etc/passwd"}, {"server_ref": "-bad"},
            {"server_ref": "bad..ref"}, {"server_port": "65536"},
            {"server_install_flasher": "maybe"},
        ):
            with self.subTest(options=options):
                self.assertTrue(server.validate_options(options))
                with self.assertRaises(ValueError):
                    server.render_files(options)

    def test_catalog_matches_role_for_both_architectures(self):
        for arch in server.ARCHES:
            url, checksum = server.image(arch)
            entry = next(item for item in IMAGE_CATALOG if item["id"] == f"playbook-server-ubuntu-noble-{arch}")
            self.assertEqual((entry["image_url"], entry["image_sha256"]), (url, checksum))
            self.assertIn(server.PROFILE, entry["profiles"])

    def test_server_options_reach_cloud_init_and_keep_flasher_disabled(self):
        env = {}
        _map_cloud_init_options_to_env(env, "standard", {
            "server_port": "9000", "server_install_flasher": False,
        })
        self.assertEqual(env["CLOUD_INIT_SERVER_PORT"], "9000")
        self.assertEqual(env["CLOUD_INIT_SERVER_INSTALL_FLASHER"], "false")
        payload = _sanitize_sd_prepare_payload({
            "profile": server.PROFILE, "distribution": "k3s", "cloud_init_options": {},
        })
        self.assertTrue(payload["config_errors"])

    def test_node_ops_only_assigns_server_module(self):
        self.assertEqual(_default_profiles_for_group(server.GROUP), [server.PROFILE])
        module = next(item for item in NODE_PROVISION_PROFILES if item["id"] == server.PROFILE)
        self.assertEqual(module["groups"], [server.GROUP])
        self.assertEqual(readable_ids(server.PROFILE), {spec["id"] for spec in server.INPUT_SPECS})
