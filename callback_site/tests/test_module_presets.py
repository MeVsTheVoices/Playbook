"""Saved module configurations: history, names, reuse across nodes, and settings drawn from SD profiles."""

from __future__ import annotations

import json

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import services.module_presets
import services.node_state
import services.sd_profiles

NODE_A = "preset-test-a"
NODE_B = "preset-test-b"
SECRET = "tskey-auth-SECRETVALUE"


class ModulePresetTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        services.module_presets._save_presets([])
        original_profiles = services.sd_profiles._load_sd_option_profiles()
        self.addCleanup(services.sd_profiles._save_sd_option_profiles, original_profiles)
        for name in (NODE_A, NODE_B):
            response = self.client.post("/api/nodes/manual-nodes", json={"name": name, "certificate": "test-cert"})
            self.assertEqual(response.status_code, 200)
            self.addCleanup(self.client.delete, f"/api/nodes/manual-nodes/{name}")

    def save_tailscale(self, node: str, **values: str) -> None:
        response = self.client.post("/api/nodes/node-inputs", json={"node": node, "profile_inputs": {"tailscale": values}})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def listing(self, module: str, node: str) -> dict:
        response = self.client.get(f"/api/nodes/module-presets?module={module}&node={node}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_every_save_is_remembered_once_and_secrets_stay_hidden(self) -> None:
        self.save_tailscale(NODE_A, tailnet="a.ts.net", auth_key=SECRET)
        self.save_tailscale(NODE_A, tailnet="a.ts.net", auth_key=SECRET)
        self.assertEqual(len(self.listing("tailscale", NODE_A)["presets"]), 1, "an identical re-save must not add a row")

        self.save_tailscale(NODE_A, tailnet="b.ts.net")
        raw = self.client.get(f"/api/nodes/module-presets?module=tailscale&node={NODE_A}").get_data(as_text=True)
        presets = json.loads(raw)["presets"]
        self.assertEqual(len(presets), 2)
        self.assertNotIn(SECRET, raw)
        newest = presets[0]
        self.assertEqual(newest["name"], "")
        self.assertTrue(any(f["id"] == "auth_key" and f["sensitive"] for f in newest["fields"]))
        self.assertTrue(any(f["id"] == "tailnet" and f["value"] == "b.ts.net" for f in newest["fields"]))
        stored = services.module_presets._load_presets()
        self.assertTrue(any(p["inputs"].get("auth_key") == SECRET for p in stored), "the real secret is kept for reapplying")

    def test_name_one_and_load_it_on_another_node(self) -> None:
        self.save_tailscale(NODE_A, tailnet="lab.ts.net", auth_key=SECRET)
        named = self.client.post("/api/nodes/module-presets", json={"node": NODE_A, "module": "tailscale", "name": "lab"})
        self.assertEqual(named.status_code, 200)
        preset_id = named.get_json()["id"]

        self.save_tailscale(NODE_A, tailnet="other.ts.net")
        self.client.post("/api/nodes/module-presets", json={"node": NODE_A, "module": "tailscale", "name": "lab"})
        presets = self.listing("tailscale", NODE_A)["presets"]
        self.assertEqual([p["name"] for p in presets if p["name"]], ["lab"], "a name is unique per module")
        self.assertNotEqual(presets[0]["id"], preset_id)

        loaded = self.client.post(
            "/api/nodes/module-presets/load",
            json={"node": NODE_B, "module": "tailscale", "source": f"preset:{presets[0]['id']}"},
        )
        self.assertEqual(loaded.status_code, 200, loaded.get_data(as_text=True))
        self.assertNotIn(SECRET, loaded.get_data(as_text=True))
        saved = services.node_state._load_node_provisioning()["node_inputs"][NODE_B]["tailscale"]
        self.assertEqual(saved["tailnet"], "other.ts.net")
        self.assertEqual(saved["auth_key"], SECRET)

    def test_other_nodes_are_offered_but_not_the_current_one(self) -> None:
        self.save_tailscale(NODE_A, tailnet="a.ts.net")
        for_b = [s["source"] for s in self.listing("tailscale", NODE_B)["sources"]]
        self.assertIn(f"node:{NODE_A}", for_b)
        for_a = [s["source"] for s in self.listing("tailscale", NODE_A)["sources"]]
        self.assertNotIn(f"node:{NODE_A}", for_a)

    def test_sd_profile_settings_are_offered_and_loadable(self) -> None:
        services.sd_profiles._save_sd_option_profiles({
            "test-sd-control": {
                "name": "test-sd-control",
                "description": "",
                "distribution": "k3s",
                "pi_only": True,
                "updated_at": "2026-11-03T20:00:00+00:00",
                "options": {
                    "k3s_role": "control-plane",
                    "k3s_allow_sd_etcd": True,
                    "k3s_config": 'server: "https://100.65.24.76:6443"\ntoken: "K10abc::server:tokensecret"\n',
                    "tailscale_enable": True,
                    "tailscale_ssh": True,
                    "tailscale_auth_key": SECRET,
                },
            }
        })
        listed = self.client.get(f"/api/nodes/module-presets?module=kubernetes&node={NODE_A}")
        text = listed.get_data(as_text=True)
        self.assertNotIn("tokensecret", text)
        source = next(s for s in listed.get_json()["sources"] if s["source"] == "sd:test-sd-control")
        self.assertEqual(source["origin"], "provisioning-dashboard")
        by_id = {f["id"]: f for f in source["fields"]}
        self.assertEqual(by_id["node_role"]["value"], "control-plane")
        self.assertEqual(by_id["server_endpoint"]["value"], "https://100.65.24.76:6443")
        self.assertTrue(by_id["server_token"]["sensitive"])
        self.assertEqual(by_id["k3s_allow_sd_etcd"]["value"], "true")

        tailscale = next(s for s in self.listing("tailscale", NODE_A)["sources"] if s["source"] == "sd:test-sd-control")
        self.assertEqual({f["id"] for f in tailscale["fields"]}, {"auth_key", "enable_ssh"})

        loaded = self.client.post(
            "/api/nodes/module-presets/load",
            json={"node": NODE_B, "module": "kubernetes", "source": "sd:test-sd-control"},
        )
        self.assertEqual(loaded.status_code, 200, loaded.get_data(as_text=True))
        state = services.node_state._load_node_provisioning()
        self.assertEqual(state["node_inputs"][NODE_B]["kubernetes"]["server_token"], "K10abc::server:tokensecret")
        self.assertIn("kubernetes", state["node_configs"][NODE_B], "loading activates the module on the node")

    def test_delete_and_error_paths(self) -> None:
        self.save_tailscale(NODE_A, tailnet="a.ts.net")
        preset_id = self.listing("tailscale", NODE_A)["presets"][0]["id"]
        self.assertEqual(self.client.delete(f"/api/nodes/module-presets/{preset_id}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/nodes/module-presets/{preset_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/nodes/module-presets?module=nope").status_code, 400)
        missing_node = self.client.post("/api/nodes/module-presets/load", json={"node": "ghost", "module": "tailscale", "source": "node:x"})
        self.assertEqual(missing_node.status_code, 404)
        gone = self.client.post("/api/nodes/module-presets/load", json={"node": NODE_A, "module": "tailscale", "source": "preset:missing"})
        self.assertEqual(gone.status_code, 404)
        no_name = self.client.post("/api/nodes/module-presets", json={"node": NODE_A, "module": "tailscale", "name": ""})
        self.assertEqual(no_name.status_code, 400)

    def test_node_ops_reset_clears_saved_configurations(self) -> None:
        self.save_tailscale(NODE_A, tailnet="a.ts.net", auth_key=SECRET)
        self.assertTrue(services.module_presets._load_presets())
        self.assertEqual(self.client.post("/api/settings/reset", json={"scope": "node_ops"}).status_code, 200)
        self.assertEqual(services.module_presets._load_presets(), [])

    def test_nodes_page_ships_the_saved_configuration_panel(self) -> None:
        text = self.page_text(self.client.get("/nodes"))
        self.assertIn('id="module-presets"', text)
        self.assertIn("Save as named configuration", text)
        self.assertIn("Draw from existing settings", text)
