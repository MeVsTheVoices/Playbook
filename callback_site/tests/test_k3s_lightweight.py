from __future__ import annotations

import copy
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

try:
    from .test_support import BaseCallbackSiteTest, callback_app, callback_core
except ImportError:
    from test_support import BaseCallbackSiteTest, callback_app, callback_core

import k3s_lightweight as lw

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = "server: https://100.65.24.76:6443\ntoken: x\n"


def _sanitize(profile: str, options: dict | None = None, config: str = BASE_CONFIG, distribution: str = "k3s") -> dict:
    return callback_core._sanitize_sd_prepare_payload(
        {
            "profile": profile,
            "distribution": distribution,
            "hostname": "t1",
            "cloud_init_options": {"k3s_config": config, **(options or {})},
        }
    )


class LightweightLogicTests(BaseCallbackSiteTest):
    def test_defaults_are_minimum_viable_and_keep_traefik(self) -> None:
        resolved = lw.resolve_lightweight({})
        self.assertEqual(resolved["disable"], ["servicelb", "metrics-server", "local-storage"])
        self.assertEqual(resolved["flags"], ["disable-network-policy"])
        self.assertEqual(resolved["ingress"], "traefik")
        self.assertEqual(resolved["errors"], [])

    def test_ingress_none_also_disables_traefik(self) -> None:
        self.assertIn("traefik", lw.resolve_lightweight({"k3s_ingress": "none"})["disable"])

    def test_ingress_nginx_is_not_offered(self) -> None:
        self.assertNotIn("ingress-nginx", lw.INGRESS_CHOICES)
        self.assertTrue(lw.resolve_lightweight({"k3s_ingress": "ingress-nginx"})["errors"])

    def test_helm_controller_requires_ingress_none(self) -> None:
        self.assertTrue(lw.resolve_lightweight({"k3s_disable_helm_controller": True})["errors"])
        self.assertEqual(
            lw.resolve_lightweight({"k3s_disable_helm_controller": True, "k3s_ingress": "none"})["errors"], []
        )

    def test_explicit_false_overrides_a_default_on_option(self) -> None:
        resolved = lw.resolve_lightweight({"k3s_disable_servicelb": False, "k3s_disable_network_policy": "false"})
        self.assertNotIn("servicelb", resolved["disable"])
        self.assertEqual(resolved["flags"], [])

    def test_pi5_starts_with_every_component_on_so_it_matches_the_nucbox(self) -> None:
        resolved = lw.resolve_lightweight({}, "pi5")
        self.assertEqual((resolved["disable"], resolved["flags"]), ([], []))
        on = lw.resolve_lightweight({"k3s_disable_servicelb": True}, "pi5")
        self.assertEqual(on["disable"], ["servicelb"])

    def test_every_role_is_defined_for_both_boards(self) -> None:
        self.assertEqual(lw.BOARDS["pi3bplus"]["node_ops_roles"], ("light-worker", "utility", "control-plane"))
        self.assertEqual(lw.BOARDS["pi5"]["node_ops_roles"], ("worker", "control-plane", "utility"))
        self.assertEqual(lw.BOARDS["pi3bplus"]["role"], "light-worker")
        self.assertEqual(lw.BOARDS["pi5"]["role"], "worker")
        for profile, board in lw.BOARDS.items():
            self.assertIn("control-plane", board["roles"], profile)
            for role in board["roles"]:
                self.assertIn(role, lw.ROLE_LABELS)
                self.assertIn(role, lw.ROLE_TAINTS)

    def test_control_plane_config_is_applied_even_if_the_text_looks_like_a_worker(self) -> None:
        text, role, _errors = lw.apply_role_to_k3s_config("server: https://x:6443\nnode-label:\n  - role=worker\n", {"k3s_role": "control-plane"}, "pi3bplus")
        self.assertEqual(role, "control-plane")
        self.assertIn("disable:", text)

    def test_user_written_disable_is_never_duplicated(self) -> None:
        text, _ = lw.apply_to_k3s_config("server: https://x:6443\ndisable:\n  - traefik\n", {})
        self.assertEqual(text.count("disable:"), 1)
        self.assertIn("  - traefik", text)


class LightweightSdPayloadTests(BaseCallbackSiteTest):
    def config(self, profile: str, options: dict | None = None) -> dict:
        return yaml.safe_load(_sanitize(profile, options)["cloud_init_options"]["k3s_config"])

    def test_pi3bplus_defaults_to_a_tainted_light_worker_not_a_server(self) -> None:
        payload = _sanitize("pi3bplus")
        data = yaml.safe_load(payload["cloud_init_options"]["k3s_config"])
        self.assertEqual(payload["cloud_init_options"]["k3s_role"], "light-worker")
        self.assertEqual(data["node-label"], ["role=light-worker"])
        self.assertEqual(data["node-taint"], [lw.ROLE_TAINTS["light-worker"]])
        self.assertEqual(data["kubelet-arg"], ["max-pods=20"])
        for server_only in ("disable", "disable-network-policy"):
            self.assertNotIn(server_only, data)

    def test_pi5_defaults_to_a_plain_worker(self) -> None:
        payload = _sanitize("pi5")
        data = yaml.safe_load(payload["cloud_init_options"]["k3s_config"])
        self.assertEqual(payload["cloud_init_options"]["k3s_role"], "worker")
        self.assertEqual(data["node-label"], ["role=worker"])
        for key in ("node-taint", "kubelet-arg", "disable", "disable-network-policy"):
            self.assertNotIn(key, data)

    def test_pi3bplus_control_plane_gets_the_lightweight_server_set(self) -> None:
        data = self.config("pi3bplus", {"k3s_role": "control-plane"})
        self.assertEqual(data["disable"], ["servicelb", "metrics-server", "local-storage"])
        self.assertTrue(data["disable-network-policy"])
        self.assertEqual(data["node-label"], ["role=control-plane"])
        self.assertEqual(data["node-taint"], [lw.ROLE_TAINTS["control-plane"]])
        self.assertEqual(data["kubelet-arg"], ["max-pods=20"])

    def test_pi5_control_plane_keeps_every_component_so_it_matches_the_nucbox(self) -> None:
        data = self.config("pi5", {"k3s_role": "control-plane"})
        self.assertEqual(data["node-label"], ["role=control-plane"])
        for key in ("disable", "disable-network-policy", "disable-cloud-controller", "disable-helm-controller", "node-taint", "kubelet-arg"):
            self.assertNotIn(key, data)

    def test_pi5_control_plane_options_are_all_selectable(self) -> None:
        data = self.config("pi5", {
            "k3s_role": "control-plane", "k3s_ingress": "none", "k3s_disable_servicelb": True,
            "k3s_disable_metrics_server": True, "k3s_disable_network_policy": True,
            "k3s_taint": True, "k3s_max_pods": "60",
        })
        self.assertEqual(data["disable"], ["servicelb", "metrics-server", "traefik"])
        self.assertTrue(data["disable-network-policy"])
        self.assertEqual(data["node-taint"], [lw.ROLE_TAINTS["control-plane"]])
        self.assertEqual(data["kubelet-arg"], ["max-pods=60"])

    def test_pi5_worker_options_are_selectable(self) -> None:
        data = self.config("pi5", {"k3s_taint": True, "k3s_max_pods": "40"})
        self.assertEqual(data["node-taint"], [lw.ROLE_TAINTS["worker"]])
        self.assertEqual(data["kubelet-arg"], ["max-pods=40"])

    def test_pi3bplus_options_can_be_turned_off(self) -> None:
        data = self.config("pi3bplus", {"k3s_taint": False})
        self.assertNotIn("node-taint", data)
        server = self.config("pi3bplus", {"k3s_role": "control-plane", "k3s_disable_servicelb": False, "k3s_disable_network_policy": False})
        self.assertNotIn("servicelb", server["disable"])
        self.assertNotIn("disable-network-policy", server)

    def test_options_of_the_other_role_are_dropped(self) -> None:
        for profile, worker in (("pi3bplus", "light-worker"), ("pi5", "worker")):
            with self.subTest(profile=profile):
                options = _sanitize(profile, {"k3s_role": worker, "k3s_disable_servicelb": True, "k3s_ingress": "none"})["cloud_init_options"]
                self.assertEqual([k for k in options if k in lw.LIGHTWEIGHT_OPTION_IDS], [])

    def test_blank_max_pods_means_the_board_default(self) -> None:
        self.assertEqual(self.config("pi3bplus", {"k3s_max_pods": ""})["kubelet-arg"], ["max-pods=20"])
        self.assertNotIn("kubelet-arg", self.config("pi5", {"k3s_max_pods": ""}))

    def test_bad_role_and_bad_max_pods_are_reported_per_board(self) -> None:
        self.assertTrue(_sanitize("pi3bplus", {"k3s_role": "utility"}).get("config_errors"), "utility is chosen via the Standard distribution on the SD form")
        self.assertTrue(_sanitize("pi3bplus", {"k3s_role": "worker"}).get("config_errors"), "a 3B+ is a light worker, not a plain worker")
        self.assertTrue(_sanitize("pi5", {"k3s_role": "light-worker"}).get("config_errors"), "a Pi 5 is a worker, not a light worker")
        for profile in ("pi3bplus", "pi5"):
            for bad in ("0", "111", "abc", "-5", "2.5"):
                with self.subTest(profile=profile, max_pods=bad):
                    self.assertTrue(_sanitize(profile, {"k3s_max_pods": bad}).get("config_errors"))

    def test_user_written_keys_win_on_every_role(self) -> None:
        for profile, role in (("pi3bplus", "light-worker"), ("pi3bplus", "control-plane"), ("pi5", "worker"), ("pi5", "control-plane")):
            with self.subTest(profile=profile, role=role):
                text, _role, _errors = lw.apply_role_to_k3s_config(
                    "server: https://x:6443\nnode-label:\n  - mine=1\nkubelet-arg:\n  - max-pods=50\nnode-taint:\n  - mine=1:NoSchedule\n",
                    {"k3s_role": role, "k3s_taint": True, "k3s_max_pods": "30"}, profile,
                )
                for key in ("node-label:", "kubelet-arg:", "node-taint:"):
                    self.assertEqual(text.count(key), 1, key)
                self.assertIn("max-pods=50", text)

    def test_role_picks_the_install_mode_on_both_boards(self) -> None:
        def mode(profile: str, role: str | None) -> str | None:
            options = {"k3s_role": role} if role else {}
            cleaned = _sanitize(profile, options)["cloud_init_options"]
            env: dict = {}
            callback_core._map_cloud_init_options_to_env(env=env, distribution="k3s", cloud_init_options=cleaned)
            return env.get("CLOUD_INIT_K3S_MODE")

        self.assertEqual(mode("pi3bplus", None), "agent")
        self.assertEqual(mode("pi3bplus", "control-plane"), "server")
        self.assertEqual(mode("pi5", None), "agent", "a default Pi 5 must be a worker, not an accidental server")
        self.assertEqual(mode("pi5", "worker"), "agent")
        self.assertEqual(mode("pi5", "control-plane"), "server")

    def test_non_pi_profiles_and_standard_distribution_get_nothing(self) -> None:
        payload = _sanitize("pi3bplus", {"k3s_disable_servicelb": True, "k3s_role": "light-worker"}, config="", distribution="standard")
        self.assertEqual(payload["cloud_init_options"].get("k3s_config", ""), "")
        for key in lw.ALL_ROLE_OPTION_IDS:
            self.assertNotIn(key, payload["cloud_init_options"])
        other = _sanitize("someboard", {"k3s_role": "control-plane", "k3s_taint": True})["cloud_init_options"]
        for key in lw.ALL_ROLE_OPTION_IDS:
            self.assertNotIn(key, other)

    def _prepare(self, profile: str, options: dict) -> tuple:
        body = {
            "profile": profile, "distribution": "k3s", "hostname": "t1", "device": "/dev/sdz",
            "ssh_public_key": "ssh-ed25519 AAAA test", "callback_url": "http://127.0.0.1:9/cb",
            "cloud_init_options": {"k3s_config": BASE_CONFIG, **options},
        }
        with patch.object(callback_app, "_ensure_prereqs", return_value=[]), patch("app.threading.Thread") as thread:
            response = self.client.post("/api/sd/prepare", json=body)
        return response, thread

    def test_prepare_endpoint_rejects_helm_off_with_traefik_on_both_boards(self) -> None:
        for profile in ("pi3bplus", "pi5"):
            with self.subTest(profile=profile):
                response, thread = self._prepare(profile, {"k3s_role": "control-plane", "k3s_disable_helm_controller": True})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Helm controller", json.dumps(response.get_json()["errors"]))
                thread.assert_not_called()

    def test_prepare_endpoint_rejects_a_bad_max_pods_value_on_both_boards(self) -> None:
        for profile in ("pi3bplus", "pi5"):
            with self.subTest(profile=profile):
                response, thread = self._prepare(profile, {"k3s_max_pods": "999"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Max pods", json.dumps(response.get_json()["errors"]))
                thread.assert_not_called()


class LightweightUiAndSpecTests(BaseCallbackSiteTest):
    def kubernetes_specs(self) -> list:
        profile = next(p for p in callback_core.NODE_PROVISION_PROFILES if p["id"] == "kubernetes")
        return profile["input_specs"]

    def test_node_ops_specs_are_scoped_to_their_roles_and_cover_both_boards(self) -> None:
        specs = {s["id"]: s for s in self.kubernetes_specs()}
        both = {"rpi5_active", "rpi_3bplus_active"}
        for option_id in lw.LIGHTWEIGHT_OPTION_IDS:
            self.assertEqual(set(specs[option_id]["only_groups"]), both, option_id)
            self.assertEqual(specs[option_id]["only_roles"], ["control-plane"], option_id)
        for option_id in lw.COMMON_OPTION_IDS:
            self.assertEqual(set(specs[option_id]["only_groups"]), both, option_id)
            self.assertEqual(specs[option_id]["only_roles"], ["worker", "light-worker", "control-plane"], option_id)
        self.assertEqual(specs["k3s_ingress"]["options"], ["traefik", "none"])

    def test_board_defaults_are_carried_per_group(self) -> None:
        specs = {s["id"]: s for s in self.kubernetes_specs()}
        for option_id in ("k3s_disable_metrics_server", "k3s_disable_local_storage", "k3s_disable_servicelb", "k3s_disable_network_policy"):
            self.assertEqual(specs[option_id]["default_by_group"], {"rpi_3bplus_active": "true", "rpi5_active": "false"}, option_id)
        for option_id in ("k3s_disable_cloud_controller", "k3s_disable_helm_controller"):
            self.assertEqual(specs[option_id]["default_by_group"], {"rpi_3bplus_active": "false", "rpi5_active": "false"}, option_id)
        self.assertEqual(specs["k3s_taint"]["default_by_group"], {"rpi_3bplus_active": "true", "rpi5_active": "false"})
        self.assertEqual(specs["k3s_max_pods"]["default_by_group"], {"rpi_3bplus_active": "20", "rpi5_active": ""})

    def test_node_role_choices_differ_per_board_and_default_to_the_small_role(self) -> None:
        role = next(s for s in self.kubernetes_specs() if s["id"] == "node_role")
        self.assertEqual(role["options_by_group"]["rpi5_active"], ["worker", "control-plane", "utility"])
        self.assertEqual(role["options_by_group"]["rpi_3bplus_active"], ["light-worker", "utility", "control-plane"])
        self.assertEqual(role["default_by_group"], {"rpi5_active": "worker", "rpi_3bplus_active": "light-worker"})

    def test_utility_role_does_not_require_a_k3s_endpoint_or_token(self) -> None:
        def missing(role: str) -> list:
            state = {"node_inputs": {"n1": {"kubernetes": {"node_role": role}}}}
            result = callback_core._missing_required_inputs("n1", ["kubernetes"], state)
            return [f["id"] for entry in result for f in entry["fields"]]

        self.assertEqual(missing("utility"), [])
        for role in ("light-worker", "worker", "control-plane"):
            self.assertEqual(sorted(missing(role)), ["server_endpoint", "server_token"], role)

    def test_home_page_role_data_covers_both_boards_and_has_no_utility_or_nginx_option(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-option-key="k3s_role"', html)
        self.assertIn('data-option-profile="pi3bplus pi5"', html)
        for option_id in lw.COMMON_OPTION_IDS + lw.LIGHTWEIGHT_OPTION_IDS:
            self.assertIn(f'data-option-key="{option_id}"', html)
        self.assertIn('data-option-role="worker light-worker control-plane"', html)
        self.assertIn('data-option-role="control-plane"', html)
        defaults = lw.ui_defaults()
        self.assertEqual(defaults["pi5"]["roles"], ["worker", "control-plane"])
        self.assertEqual(defaults["pi3bplus"]["roles"], ["light-worker", "control-plane"])
        self.assertFalse(any(defaults["pi5"]["server"].values()), "Pi 5 starts with every lightweight option off")
        self.assertTrue(defaults["pi3bplus"]["server"]["k3s_disable_servicelb"])
        self.assertNotIn('<option value="utility"', html)
        self.assertNotIn("ingress-nginx</option>", html)

    def test_node_ops_page_data_path_carries_the_module_specs(self) -> None:
        # The page builds its module forms from /api/state/source-of-truth, not /api/nodes/profiles.
        state = self.client.get("/api/state/source-of-truth").get_json()["state"]
        kube = next(p for p in state["profiles"] if p["id"] == "kubernetes")
        specs = {s["id"]: s for s in kube["input_specs"]}
        self.assertEqual(specs["node_role"]["options_by_group"]["rpi_3bplus_active"], ["light-worker", "utility", "control-plane"])
        self.assertEqual(specs["node_role"]["options_by_group"]["rpi5_active"], ["worker", "control-plane", "utility"])
        self.assertEqual(specs["k3s_taint"]["only_roles"], ["worker", "light-worker", "control-plane"])
        self.assertEqual(kube["groups"], ["rpi5_active", "rpi_3bplus_active"])
        for profile in state["profiles"]:
            self.assertIn("input_specs", profile, profile["id"])
        node_records = state["nodes"]
        for record in node_records:
            for how in record.get("provision_how", []):
                self.assertNotIn("input_specs", how, "per-node records must stay lean")

    def test_nodes_page_parses_string_defaults_instead_of_treating_false_as_true(self) -> None:
        html = self.client.get("/nodes").get_data(as_text=True)
        self.assertIn("String(groupDefault(spec, selectedNode.group)).toLowerCase()", html)
        self.assertNotIn(": Boolean(spec.default);", html)

    def test_nodes_page_filters_specs_by_group_and_saves_explicit_false(self) -> None:
        html = self.client.get("/nodes").get_data(as_text=True)
        self.assertIn("spec.only_groups.includes(selectedNode.group)", html)
        self.assertIn("data-only-roles", html)
        self.assertIn("options_by_group", html)
        self.assertIn("default_by_group", html)
        self.assertIn('input.checked ? "true" : "false"', html)

    def test_old_option_names_are_gone_everywhere(self) -> None:
        for relative in (
            "roles/raspberry_pi5/tasks/main.yml", "roles/raspberry_pi_3bplus/tasks/main.yml",
            "callback_site/templates/index.html", "callback_site/templates/nodes.html", "callback_site/k3s_lightweight.py",
        ):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for stale in ("k3s_light_taint", "k3s_light_max_pods", "light_taint", "light_max_pods"):
                self.assertNotIn(stale, text, f"{relative} still uses {stale}")


class NodeOpsFalseOkTests(BaseCallbackSiteTest):
    def run_with_ansible_output(self, returncode: int, stdout: str) -> dict:
        from types import SimpleNamespace

        profile = next(p for p in callback_core.NODE_PROVISION_PROFILES if p["id"] == "baseline")
        with patch.object(callback_core.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")), \
                patch.object(callback_core, "_ssh_private_key_preflight", return_value=""), \
                patch.object(callback_core, "_seed_known_host"):
            return callback_core._run_node_profile(
                "node-x", profile, "apply", True, {},
                node_group="rpi_3bplus_active", node_address="192.0.2.10",
                node_username="jdizzle", node_certificate="rack-bootstrap-ed25519",
            )

    def test_no_hosts_matched_is_failed_even_though_ansible_exits_zero(self) -> None:
        result = self.run_with_ansible_output(
            0, "[WARNING]: Could not match supplied host pattern, ignoring: node-x\nskipping: no hosts matched\n\nPLAY RECAP ****\n"
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("No hosts matched", result["output"])

    def test_a_normal_clean_run_is_still_ok(self) -> None:
        result = self.run_with_ansible_output(0, "PLAY RECAP\nnode-x : ok=12 changed=1 unreachable=0 failed=0\n")
        self.assertEqual(result["status"], "ok")


class LightweightAnsibleRoleTests(BaseCallbackSiteTest):
    # Task names differ per board; the harness runs the real role tasks, not a copy of their logic.
    BOARDS = {
        "pi3bplus": {
            "role_dir": "raspberry_pi_3bplus",
            "prefix": "raspberry_pi_3bplus",
            "k3s_roles": ("light-worker", "control-plane"),
            "tasks": (
                "Resolve structured K3s settings",
                "Validate 3B+ role",
                "Resolve lightweight K3s component list",
                "Validate lightweight K3s options",
                "Validate 3B+ control-plane settings",
                "Validate 3B+ light-worker settings",
                "Validate 3B+ max pods",
                "Write structured K3s control-plane config",
                "Write structured K3s light-worker config",
            ),
        },
        "pi5": {
            "role_dir": "raspberry_pi5",
            "prefix": "raspberry_pi5",
            "k3s_roles": ("worker", "control-plane"),
            "tasks": (
                "Resolve structured K3s settings",
                "Validate Pi 5 role",
                "Resolve lightweight K3s component list",
                "Validate lightweight K3s options",
                "Validate Pi 5 control-plane settings",
                "Validate Pi 5 worker settings",
                "Validate Pi 5 max pods",
                "Write structured K3s control-plane config",
                "Write structured K3s worker config",
            ),
        },
    }

    def render_role(self, extra_inputs: dict, profile: str = "pi3bplus") -> tuple[int, str, dict]:
        board = self.BOARDS[profile]
        prefix = board["prefix"]
        ansible = REPO_ROOT / ".venv" / "bin" / "ansible-playbook"
        if not ansible.exists():
            self.skipTest("repo ansible-playbook not available")
        role_tasks = yaml.safe_load((REPO_ROOT / "roles" / board["role_dir"] / "tasks" / "main.yml").read_text(encoding="utf-8"))
        by_name = {t["name"]: t for t in role_tasks if isinstance(t, dict) and "name" in t}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "config.yaml"
            selected = []
            for name in board["tasks"]:
                task = copy.deepcopy(by_name[name])
                task.pop("tags", None)
                task.pop("no_log", None)
                if name.startswith("Write structured"):
                    content = task["ansible.builtin.copy"]["content"]
                    task["ansible.builtin.copy"] = {"dest": str(out), "content": content}
                selected.append(task)
            play = [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "rack_manage_with_ansible": True,
                        "provision_profile": "kubernetes",
                        "inventory_hostname": "test-node",
                        f"{prefix}_tailscale_ip": {"stdout_lines": ["100.70.57.110"]},
                        f"{prefix}_k3s_server_unit": {"stat": {"exists": False}},
                        "provision_inputs": {
                            key: value
                            for key, value in {
                                "node_role": "control-plane",
                                "server_endpoint": "https://100.65.24.76:6443",
                                "server_token": "K10abc::server:1e81",
                                **extra_inputs,
                            }.items()
                            if value is not None  # None means the input is not set at all
                        },
                    },
                    "tasks": selected,
                }
            ]
            play_file = Path(tmp) / "play.yml"
            play_file.write_text(yaml.safe_dump(play), encoding="utf-8")
            result = subprocess.run(
                [str(ansible), "-i", "localhost,", "-c", "local", str(play_file)],
                capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
            )
            data = yaml.safe_load(out.read_text(encoding="utf-8")) if out.exists() else {}
        return result.returncode, result.stdout + result.stderr, data

    def assert_role_matches_python(self, profile: str, role: str, inputs: dict) -> None:
        expected_text, _role, errors = lw.apply_role_to_k3s_config("", {"k3s_role": role, **inputs}, profile)
        code, output, data = self.render_role({"node_role": role, **inputs}, profile)
        if errors:
            self.assertNotEqual(code, 0, f"{profile}/{role}: role accepted inputs Python rejects: {inputs}")
            return
        self.assertEqual(code, 0, f"{profile}/{role} {inputs}: {output[-800:]}")
        expected = yaml.safe_load(expected_text)
        for key in ("node-label", "node-taint", "kubelet-arg", "disable", "disable-network-policy", "disable-cloud-controller", "disable-helm-controller"):
            self.assertEqual(data.get(key), expected.get(key), f"{profile}/{role} {inputs}: {key}")
        self.assertEqual(data["node-ip"], "100.70.57.110")
        self.assertEqual(("advertise-address" in data), role == "control-plane")

    def test_roles_match_python_rules_on_both_boards(self) -> None:
        common = ({}, {"k3s_taint": "false"}, {"k3s_taint": "true"}, {"k3s_max_pods": "35"}, {"k3s_max_pods": ""}, {"k3s_max_pods": "110"})
        server_only = (
            {"k3s_ingress": "none"},
            {"k3s_disable_servicelb": "false", "k3s_disable_metrics_server": "false", "k3s_disable_local_storage": "false", "k3s_disable_network_policy": "false"},
            {"k3s_disable_servicelb": "true", "k3s_disable_metrics_server": "true", "k3s_disable_local_storage": "true", "k3s_disable_network_policy": "true"},
            {"k3s_ingress": "none", "k3s_disable_helm_controller": "true", "k3s_disable_cloud_controller": "true"},
            {"k3s_disable_helm_controller": "true"},
            {"k3s_ingress": "ingress-nginx"},
        )
        for profile, board in self.BOARDS.items():
            for role in board["k3s_roles"]:
                for inputs in common + (server_only if role == "control-plane" else ()):
                    with self.subTest(profile=profile, role=role, inputs=inputs):
                        self.assert_role_matches_python(profile, role, inputs)

    def test_defaults_differ_per_board_exactly_as_python_says(self) -> None:
        pi3 = self.render_role({"node_role": "control-plane"}, "pi3bplus")[2]
        pi5 = self.render_role({"node_role": "control-plane"}, "pi5")[2]
        self.assertEqual(pi3["disable"], ["servicelb", "metrics-server", "local-storage"])
        self.assertNotIn("disable", pi5, "a default Pi 5 control-plane keeps every component, like the NucBox")
        self.assertEqual(pi3["kubelet-arg"], ["max-pods=20"])
        self.assertNotIn("kubelet-arg", pi5)
        self.assertIn("node-taint", pi3)
        self.assertNotIn("node-taint", pi5)

    def test_worker_configs_are_never_server_configs(self) -> None:
        for profile, role in (("pi3bplus", "light-worker"), ("pi5", "worker")):
            code, output, data = self.render_role({"node_role": role}, profile)
            self.assertEqual(code, 0, output[-800:])
            for server_only in ("disable", "advertise-address", "secrets-encryption", "disable-network-policy"):
                self.assertNotIn(server_only, data, f"{profile}/{role}")
            self.assertEqual(data["token"], "K10abc::server:1e81")

    def test_unset_role_defaults_to_the_small_role_never_control_plane(self) -> None:
        for profile, label in (("pi3bplus", lw.ROLE_LABELS["light-worker"]), ("pi5", lw.ROLE_LABELS["worker"])):
            code, output, data = self.render_role({"node_role": None}, profile)
            self.assertEqual(code, 0, output[-800:])
            self.assertEqual(data["node-label"], [label], profile)
            self.assertNotIn("advertise-address", data)

    def test_blank_role_is_rejected_not_guessed(self) -> None:
        for profile in self.BOARDS:
            code, output, _data = self.render_role({"node_role": ""}, profile)
            self.assertNotEqual(code, 0, output[-400:])
            self.assertIn("must be", output)

    def test_utility_role_writes_no_kubernetes_config_and_needs_no_token_on_both_boards(self) -> None:
        for profile in self.BOARDS:
            code, output, data = self.render_role({"node_role": "utility", "server_endpoint": "", "server_token": ""}, profile)
            self.assertEqual(code, 0, output[-800:])
            self.assertEqual(data, {}, f"{profile}: utility must not write a K3s config")

    def test_invalid_roles_and_bad_pod_caps_fail_with_a_readable_message(self) -> None:
        cases = (
            ("pi3bplus", {"node_role": "worker"}, "must be"),
            ("pi5", {"node_role": "light-worker"}, "must be"),
            ("pi3bplus", {"node_role": "light-worker", "k3s_max_pods": "0"}, "Max pods"),
            ("pi3bplus", {"node_role": "control-plane", "k3s_max_pods": "111"}, "Max pods"),
            ("pi5", {"node_role": "worker", "k3s_max_pods": "abc"}, "Max pods"),
            ("pi5", {"node_role": "control-plane", "k3s_max_pods": "-1"}, "Max pods"),
            ("pi3bplus", {"node_role": "light-worker", "server_token": ""}, "server token"),
            ("pi5", {"node_role": "worker", "server_token": ""}, "server token"),
            ("pi5", {"node_role": "control-plane", "server_token": ""}, "server token"),
        )
        for profile, inputs, needle in cases:
            with self.subTest(profile=profile, inputs=inputs):
                code, output, data = self.render_role(inputs, profile)
                self.assertNotEqual(code, 0)
                self.assertIn(needle, output)
                self.assertEqual(data, {})

    def test_server_only_options_do_not_block_a_worker(self) -> None:
        for profile, role in (("pi3bplus", "light-worker"), ("pi5", "worker")):
            code, output, data = self.render_role({"node_role": role, "k3s_disable_helm_controller": "true"}, profile)
            self.assertEqual(code, 0, output[-800:])
            self.assertNotIn("disable-helm-controller", data)

    def test_labels_and_taints_in_the_roles_match_the_python_constants(self) -> None:
        for relative, roles in (("raspberry_pi_3bplus", ("light-worker", "control-plane")), ("raspberry_pi5", ("worker", "control-plane"))):
            text = (REPO_ROOT / "roles" / relative / "tasks" / "main.yml").read_text(encoding="utf-8")
            for role in roles:
                self.assertIn(f"'{lw.ROLE_LABELS[role]}'", text, f"{relative}: {role} label")
                self.assertIn(f"'{lw.ROLE_TAINTS[role]}'", text, f"{relative}: {role} taint")
