from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import k3s_lightweight as lw
import tailscale_policy as tp
try:
    from .sd_script import read_sd_script
except ImportError:
    from sd_script import read_sd_script

REPO_ROOT = Path(__file__).resolve().parents[2]
# profile -> (group_vars directory, role variable prefix)
GROUP_VARS = {
    "pi5": ("rpi5", "raspberry_pi5"),
    "pi4": ("rpi4", "raspberry_pi4"),
    "pi3bplus": ("rpi_3bplus", "raspberry_pi_3bplus"),
}


def build(**overrides):
    options = {"admin": "group:admins", "homelab_tag": "tag:homelab", "ssh_user": "jdizzle"}
    options.update(overrides)
    return tp.build_policy(options)


def all_tags(node) -> set:
    """Every tag:... string anywhere in a structure, including host:port forms."""
    found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found |= all_tags(key) | all_tags(value)
    elif isinstance(node, list):
        for item in node:
            found |= all_tags(item)
    elif isinstance(node, str):
        match = re.match(r"^(tag:[A-Za-z][A-Za-z0-9-]*)(?::\d+)?$", node)
        if match:
            found.add(match.group(1))
    return found


class TailscalePolicyBuilderTests(BaseCallbackSiteTest):
    def test_default_policy_covers_every_board_and_the_homelab_tag(self) -> None:
        result = build()
        self.assertTrue(result["ok"], result["errors"])
        expected = {board["tailscale_tag"] for board in lw.BOARDS.values()} | {"tag:homelab"}
        self.assertEqual(set(result["policy"]["tagOwners"]), expected)
        self.assertEqual(set(result["summary"]["tags"]), expected)

    def test_every_tag_used_anywhere_is_defined_in_tag_owners(self) -> None:
        for overrides in ({}, {"traffic": "all"}, {"pi_servers": True, "exit_node": True, "subnets": "10.0.0.0/24", "subnet_access": True, "deny_tests": True}):
            with self.subTest(overrides=overrides):
                policy = build(**overrides)["policy"]
                used = all_tags({key: value for key, value in policy.items() if key != "tagOwners"})
                self.assertLessEqual(used, set(policy["tagOwners"]))

    def test_k3s_ports_are_the_documented_ones_and_etcd_is_opt_in(self) -> None:
        rack = build()["policy"]["grants"][1]["ip"]
        for port in ("tcp:6443", "tcp:10250", "udp:8472"):
            self.assertIn(port, rack)
        self.assertNotIn("tcp:2379-2380", rack, "etcd is only needed between servers")
        self.assertIn("tcp:2379-2380", build(pi_servers=True)["policy"]["grants"][1]["ip"])

    def test_all_traffic_mode_opens_every_port_and_warns(self) -> None:
        result = build(traffic="all")
        self.assertEqual(result["policy"]["grants"][1]["ip"], ["*"])
        self.assertTrue(any(w["level"] == "warn" and "Every port" in w["text"] for w in result["warnings"]))

    def test_uses_grants_not_legacy_acls(self) -> None:
        policy = build()["policy"]
        self.assertIn("grants", policy)
        self.assertNotIn("acls", policy)

    def test_a_blank_board_tag_drops_that_board(self) -> None:
        result = build(board_tags={"pi4": ""})
        self.assertNotIn("tag:rpi4", result["policy"]["tagOwners"])
        self.assertIn("tag:rpi5", result["policy"]["tagOwners"])

    def test_ssh_is_nonroot_by_default_and_root_is_a_warned_opt_in(self) -> None:
        self.assertEqual(build()["policy"]["ssh"][0]["users"], ["autogroup:nonroot"])
        root = build(ssh_root=True)
        self.assertEqual(root["policy"]["ssh"][0]["users"], ["autogroup:nonroot", "root"])
        self.assertTrue(any("Root login" in w["text"] for w in root["warnings"]))
        self.assertNotIn("ssh", build(ssh=False)["policy"])

    def test_routes_and_exit_nodes_are_auto_approved_for_the_rack_tags_only(self) -> None:
        result = build(subnets="192.168.174.0/24, 192.168.175.0/24", exit_node=True)
        approvers = result["policy"]["autoApprovers"]
        self.assertEqual(set(approvers["routes"]), {"192.168.174.0/24", "192.168.175.0/24"})
        self.assertEqual(set(approvers["exitNode"]), set(result["summary"]["tags"]))
        for tags in approvers["routes"].values():
            self.assertEqual(set(tags), set(result["summary"]["tags"]))
        self.assertTrue(any("first advertised" in w["text"] for w in result["warnings"]))
        self.assertIn({"src": ["group:admins"], "dst": ["autogroup:internet"], "ip": ["*"]}, result["policy"]["grants"])
        self.assertNotIn("autoApprovers", build(subnets="")["policy"])

    def test_rack_nodes_reach_subnets_only_when_asked(self) -> None:
        plain = build(subnets="10.0.0.0/24")["policy"]["grants"]
        self.assertEqual(len(plain), 2)
        with_access = build(subnets="10.0.0.0/24", subnet_access=True)["policy"]["grants"]
        self.assertEqual(with_access[2]["dst"], ["10.0.0.0/24"])

    def test_tests_follow_the_tailscale_assertion_rules(self) -> None:
        policy = build(deny_tests=True, subnets="192.168.174.0/24")["policy"]
        entries = policy["tests"] + policy["sshTests"]
        for test in entries:
            self.assertFalse(test["src"].startswith("autogroup:"), "test sources must be a user, group, tag or host")
            for key in ("accept", "deny"):
                for destination in test.get(key, []):
                    self.assertNotIn("*", destination, "tests do not support wildcards")
                    self.assertNotIn("/", destination, "tests do not support CIDR ranges")
        icmp = [t for t in policy["tests"] if t.get("proto") == "icmp"]
        self.assertTrue(icmp and all(d.endswith(":0") for t in icmp for d in t["accept"]), "ICMP tests use port 0")

    def test_deny_tests_are_opt_in_and_never_probe_a_granted_port(self) -> None:
        plain = build()["policy"]
        self.assertFalse(any("deny" in t for t in plain["tests"] + plain["sshTests"]))
        denied = build(deny_tests=True, extra_ports="tcp:23")["policy"]
        probes = [d for t in denied["tests"] for d in t.get("deny", [])]
        self.assertEqual(probes, ["tag:homelab:3389"], "23 is granted here, so the probe moves on")

    def test_admin_autogroup_skips_tests_that_need_a_user_or_group_source(self) -> None:
        result = build(admin="autogroup:admin")
        self.assertTrue(result["ok"])
        self.assertFalse(any(t["src"].startswith("autogroup:") for t in result["policy"]["tests"]))
        self.assertNotIn("sshTests", result["policy"])
        self.assertEqual(result["policy"]["tagOwners"]["tag:homelab"], ["autogroup:admin"])

    def test_group_admin_needs_a_definition_and_members_emit_it(self) -> None:
        without = build()
        self.assertNotIn("groups", without["policy"])
        self.assertTrue(any("must exist under 'groups'" in w["text"] for w in without["warnings"]))
        with_members = build(admin_members="a@example.com\nb@example.com")
        self.assertEqual(with_members["policy"]["groups"], {"group:admins": ["a@example.com", "b@example.com"]})
        self.assertFalse(any("must exist under 'groups'" in w["text"] for w in with_members["warnings"]))

    def test_tests_can_be_switched_off(self) -> None:
        policy = build(tests=False)["policy"]
        self.assertNotIn("tests", policy)
        self.assertNotIn("sshTests", policy)

    def test_invalid_inputs_are_rejected_with_a_reason(self) -> None:
        bad = {
            "admin": "bob",
            "homelab_tag": "homelab",
            "extra_ports": "tcp:99999,foo",
            "subnets": "not-a-cidr",
            "admin_members": "nope",
            "ssh_user": "root",
            "traffic": "some",
        }
        result = tp.build_policy(bad)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["policy"])
        text = " ".join(result["errors"])
        for needle in ("Admin 'bob'", "Homelab tag", "tcp:99999", "foo", "not-a-cidr", "nope", "root", "Rack traffic"):
            self.assertIn(needle, text)
        self.assertFalse(tp.build_policy({})["ok"], "an admin principal is required")

    def test_port_and_subnet_forms_are_normalised(self) -> None:
        result = build(extra_ports="TCP:8088 udp:5000-5010", subnets="192.168.174.7/24")
        self.assertIn("tcp:8088", result["policy"]["grants"][1]["ip"])
        self.assertIn("udp:5000-5010", result["policy"]["grants"][1]["ip"])
        self.assertEqual(result["summary"]["routes"], 1)
        self.assertIn("192.168.174.0/24", result["policy"]["autoApprovers"]["routes"])

    def test_policy_text_is_valid_json_in_reading_order(self) -> None:
        result = build(subnets="10.0.0.0/24")
        parsed = json.loads(result["policy_text"])
        self.assertEqual(parsed, json.loads(json.dumps(result["policy"])))
        self.assertEqual(list(parsed)[:3], ["tagOwners", "grants", "ssh"])

    def test_policy_text_keeps_comma_separated_values_on_one_line(self) -> None:
        result = build(subnets="10.0.0.0/24", admin_members="a@example.com,b@example.com")
        text = result["policy_text"]
        policy = json.loads(text)
        for values in (
            policy["groups"]["group:admins"],
            policy["grants"][1]["src"],
            policy["grants"][1]["ip"],
            policy["ssh"][0]["users"],
        ):
            self.assertIn(json.dumps(values), text)
        self.assertIn('"grants": [\n    {', text)
        self.assertLess(len(text.splitlines()), len(json.dumps(policy, indent=2).splitlines()))
        self.assertEqual(policy, result["policy"])

    def test_auto_approver_route_tags_share_a_line(self) -> None:
        result = build(subnets="192.168.174.0/24, 192.168.175.0/24")
        for cidr, tags in result["policy"]["autoApprovers"]["routes"].items():
            expected = f'      {json.dumps(cidr)}: [\n        {", ".join(json.dumps(tag) for tag in tags)}\n      ]'
            self.assertIn(expected, result["policy_text"])

    def test_the_nucbox_homelab_tag_is_a_source_and_destination_of_the_rack_rule(self) -> None:
        rack = build()["policy"]["grants"][1]
        self.assertIn("tag:homelab", rack["src"])
        self.assertIn("tag:homelab", rack["dst"])


class TailscaleDefaultsMatchTheRackTests(BaseCallbackSiteTest):
    """The policy page, the SD script and the Ansible group_vars must agree on the tags."""

    def test_every_board_has_a_label_and_a_tag(self) -> None:
        for profile, board in lw.BOARDS.items():
            self.assertTrue(board["label"], profile)
            self.assertRegex(board["tailscale_tag"], r"^tag:[a-z0-9-]+$")
        self.assertEqual(len({b["tailscale_tag"] for b in lw.BOARDS.values()}), len(lw.BOARDS))

    def test_group_vars_advertise_the_registry_tag_plus_homelab(self) -> None:
        self.assertEqual(set(GROUP_VARS), set(lw.BOARDS), "every board needs a group_vars entry")
        for profile, (directory, prefix) in GROUP_VARS.items():
            data = yaml.safe_load((REPO_ROOT / "group_vars" / directory / "all.yml").read_text(encoding="utf-8"))
            self.assertEqual(
                data[f"{prefix}_tailscale_tags"], [lw.BOARDS[profile]["tailscale_tag"], tp.HOMELAB_TAG], profile
            )

    def test_sd_script_defaults_to_the_registry_tag_plus_homelab(self) -> None:
        script = read_sd_script()
        for profile, board in lw.BOARDS.items():
            self.assertIn(f'CLOUD_INIT_TAILSCALE_TAGS="{board["tailscale_tag"]},{tp.HOMELAB_TAG}"', script, profile)

    def test_role_defaults_use_the_registry_tag(self) -> None:
        for profile, role_dir in (("pi5", "raspberry_pi5"), ("pi4", "raspberry_pi4")):
            data = yaml.safe_load((REPO_ROOT / "roles" / role_dir / "defaults" / "main.yml").read_text(encoding="utf-8"))
            self.assertEqual(data[f"{role_dir}_tailscale_tags"], [lw.BOARDS[profile]["tailscale_tag"]])

    def test_inventory_and_site_cover_every_board_group(self) -> None:
        inventory = (REPO_ROOT / "inventory.yml").read_text(encoding="utf-8")
        site = (REPO_ROOT / "site.yml").read_text(encoding="utf-8")
        for board in lw.BOARDS.values():
            self.assertIn(f"{board['group']}:", inventory)
            self.assertIn(f"hosts: {board['group']}", site)


class TailscalePolicyApiAndPageTests(BaseCallbackSiteTest):
    def test_policy_endpoint_builds_a_policy(self) -> None:
        response = self.client.post("/api/tailscale/policy", json={"admin": "group:admins", "homelab_tag": "tag:homelab"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("tag:rpi4", data["policy"]["tagOwners"])
        self.assertEqual(list(json.loads(data["policy_text"]))[0], "tagOwners")
        self.assertTrue(data["rules"])

    def test_policy_endpoint_returns_400_with_reasons_for_bad_input(self) -> None:
        response = self.client.post("/api/tailscale/policy", json={"admin": "group:admins", "homelab_tag": "bad"})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["errors"])
        self.assertEqual(self.client.post("/api/tailscale/policy", data="not json", content_type="text/plain").status_code, 400)
        self.assertEqual(self.client.post("/api/tailscale/policy", json=["list"]).status_code, 400)

    def test_inputs_fragment_has_a_tag_field_per_board_and_no_dead_api_key_field(self) -> None:
        html = self.client.get("/fragments/tailscale-acl/inputs").get_data(as_text=True)
        for profile, board in lw.BOARDS.items():
            self.assertIn(f'data-board-tag="{profile}"', html)
            self.assertIn(f'value="{board["tailscale_tag"]}"', html)
        self.assertNotIn("tailscale_api_key", html)
        self.assertNotIn("Save Tailscale API Settings", html)
        for field in ("traffic", "pi_servers", "extra_ports", "subnets", "subnet_access", "exit_node", "ssh", "ssh_root", "ssh_user", "tests", "deny_tests", "admin_members"):
            self.assertIn(f'id="{field}"', html, field)

    def test_homelab_tag_default_is_the_real_tag_not_derived_from_the_rack_name(self) -> None:
        self.client.post("/api/settings", json={"rack_name": "Rack Prime"})
        html = self.client.get("/fragments/tailscale-acl/inputs").get_data(as_text=True)
        self.assertIn('id="homelab_tag" value="tag:homelab"', html)
        self.assertNotIn("tag:rack-prime", html)

    def test_output_and_overview_fragments_have_the_elements_the_script_fills(self) -> None:
        output = self.client.get("/fragments/tailscale-acl/output").get_data(as_text=True)
        for element in ("policy_output", "policy_status", "policy_warnings", "policy_rules", "copy_policy", "download_policy"):
            self.assertIn(f'id="{element}"', output, element)
        self.assertRegex(output, r'<div class="policy-code-wrap">\s*<button id="copy_policy"')
        overview = self.client.get("/fragments/tailscale-acl/overview").get_data(as_text=True)
        for element in ("ov-rack", "ov-tailnet", "ov-tags", "ov-grants", "ov-route-count", "ov-tests"):
            self.assertIn(f'id="{element}"', overview, element)
        self.assertNotIn("No API calls required", overview)

    def test_apply_guide_covers_the_rollout_gotchas(self) -> None:
        guide = self.client.get("/fragments/tailscale-acl/apply-guide").get_data(as_text=True)
        for phrase in ("tagOwners", "first advertised", "--ssh", "allow-all"):
            self.assertIn(phrase, guide)

    def test_page_script_uses_the_server_side_builder(self) -> None:
        script = (REPO_ROOT / "callback_site" / "static" / "tailscale_acl.js").read_text(encoding="utf-8")
        self.assertIn("/api/tailscale/policy", script)
        self.assertNotIn("rpi5_tag", script)
        self.assertNotIn("pi5_subnets", script)
        self.assertNotIn("tailscale_api_key", script)
