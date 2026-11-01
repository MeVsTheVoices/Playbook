from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "k3s_fresh_start.sh"

FAKE_SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >> "$FAKE_LOG"
case "$1" in
  cat) exit 0 ;;
  show) echo '{ path=/usr/local/bin/k3s ; argv[]=/usr/local/bin/k3s server --cluster-init --tls-san=100.65.24.76 --node-ip=100.65.24.76 ; ignore_errors=no }' ;;
esac
exit 0
"""

FAKE_K3S = r"""#!/bin/bash
echo "k3s $*" >> "$FAKE_LOG"
if [[ "$1" == "server" ]]; then
  if [[ "${FAKE_RESET_FAIL:-}" == "1" ]]; then echo "etcd exploded"; exit 1; fi
  echo "Managed etcd cluster membership has been reset, restart without --cluster-reset flag now."
  exit 0
fi
if [[ "$1" == "kubectl" ]]; then
  shift
  case "$*" in
    "get --raw=/readyz") exit 0 ;;
    "get nodes -o name") printf 'node/%s\nnode/rack-alpha\nnode/rack-bravo\n' "$(hostname | tr '[:upper:]' '[:lower:]')" ;;
    delete\ node*) echo "deleted: $*" ;;
    *) echo "(kubectl $*)" ;;
  esac
fi
exit 0
"""


class K3sFreshStartTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("unshare") is None or subprocess.run(["unshare", "-r", "true"], capture_output=True).returncode != 0:
            self.skipTest("unprivileged user namespaces unavailable")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in {"systemctl": FAKE_SYSTEMCTL, "k3s": FAKE_K3S, "sleep": "#!/bin/bash\nexit 0\n"}.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.data = root / "data"
        (self.data / "server" / "db").mkdir(parents=True)
        (self.data / "server" / "db" / "etcd.marker").write_text("original datastore", encoding="utf-8")
        (self.data / "server" / "token").write_text("K10secret::server:abc", encoding="utf-8")
        self.dropin_dir = root / "config.yaml.d"
        self.backup_root = root / "backups"
        self.backup_root.mkdir()
        self.log_dir = root / "logs"
        self.log_dir.mkdir()
        self.fake_log = root / "calls.log"

    def run_script(self, *args: str, reset_fail: bool = False) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_LOG": str(self.fake_log),
            "K3S_BIN": str(self.bin / "k3s"),
            "K3S_DATA_DIR": str(self.data),
            "K3S_DROPIN_DIR": str(self.dropin_dir),
            "K3S_BACKUP_ROOT": str(self.backup_root),
            "K3S_LOG_DIR": str(self.log_dir),
        }
        if reset_fail:
            env["FAKE_RESET_FAIL"] = "1"
        return subprocess.run(
            ["unshare", "-r", "bash", str(SCRIPT), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def calls(self) -> list[str]:
        return self.fake_log.read_text(encoding="utf-8").splitlines() if self.fake_log.exists() else []

    def test_dry_run_changes_nothing_and_needs_no_root(self) -> None:
        result = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--lightweight"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disable-network-policy: true", result.stdout)
        self.assertIn("Dry run: nothing changed", result.stdout)

    def test_default_run_does_not_impose_server_flags_on_the_nucbox(self) -> None:
        dry = subprocess.run(["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=60)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertNotIn("disable:", dry.stdout)
        self.assertNotIn("10-lightweight.yaml", dry.stdout)

    def test_options_require_the_lightweight_flag(self) -> None:
        result = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--option", "k3s_ingress=none"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--lightweight", result.stderr)

    def test_default_full_run_resets_without_writing_a_dropin(self) -> None:
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-500:])
        self.assertFalse(self.dropin_dir.exists(), "uninstalling Traefik and friends must be opt-in")
        calls = self.calls()
        self.assertLess(calls.index("systemctl stop k3s"), next(i for i, c in enumerate(calls) if "--cluster-reset" in c))
        deleted = next(c for c in calls if "kubectl delete node" in c)
        self.assertIn("rack-alpha", deleted)
        self.assertNotIn(os.uname().nodename.lower(), deleted, "must never delete this server's own node")

    def test_full_run_with_lightweight_backs_up_writes_dropin_resets_and_removes_only_other_nodes(self) -> None:
        result = self.run_script("--yes", "--lightweight")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-500:])

        backups = list(self.backup_root.glob("k3s-fresh-start-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "db" / "etcd.marker").read_text(encoding="utf-8"), "original datastore")
        self.assertTrue((backups[0] / "token").exists())

        data = yaml.safe_load((self.dropin_dir / "10-lightweight.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["disable"], ["servicelb", "metrics-server", "local-storage"])
        self.assertTrue(data["disable-network-policy"])

        calls = self.calls()
        stop = calls.index("systemctl stop k3s")
        reset = next(i for i, c in enumerate(calls) if "--cluster-reset" in c)
        start = calls.index("systemctl start k3s")
        self.assertLess(stop, reset)
        self.assertLess(reset, start)
        self.assertIn("--cluster-init", calls[reset])
        self.assertIn("--node-ip=100.65.24.76", calls[reset], "the reset must reuse the unit's own flags")

        deleted = next(c for c in calls if "kubectl delete node" in c)
        self.assertIn("rack-alpha", deleted)
        self.assertIn("rack-bravo", deleted)
        self.assertNotIn(os.uname().nodename.lower(), deleted, "must never delete this server's own node")

    def test_keep_nodes_skips_deletion(self) -> None:
        result = self.run_script("--yes", "--keep-nodes")
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        self.assertFalse(any("delete node" in c for c in self.calls()))

    def test_options_flow_into_the_dropin(self) -> None:
        result = self.run_script("--yes", "--lightweight", "--option", "k3s_ingress=none", "--option", "k3s_disable_metrics_server=false")
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        data = yaml.safe_load((self.dropin_dir / "10-lightweight.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["disable"], ["servicelb", "local-storage", "traefik"])

    def test_failed_reset_stops_before_start_and_keeps_the_backup(self) -> None:
        result = self.run_script("--yes", reset_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("To roll back", result.stdout + result.stderr)
        calls = self.calls()
        self.assertNotIn("systemctl start k3s", calls, "must not start K3s on a half-reset datastore")
        self.assertFalse(any("delete node" in c for c in calls))
        backups = list(self.backup_root.glob("k3s-fresh-start-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "db").is_dir())

    def test_refuses_without_confirmation(self) -> None:
        env_result = subprocess.run(
            ["unshare", "-r", "bash", str(SCRIPT)],
            input="no\n", capture_output=True, text=True, timeout=60,
            env={
                "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.fake_log), "K3S_BIN": str(self.bin / "k3s"),
                "K3S_DATA_DIR": str(self.data), "K3S_DROPIN_DIR": str(self.dropin_dir),
                "K3S_BACKUP_ROOT": str(self.backup_root), "K3S_LOG_DIR": str(self.log_dir),
            },
        )
        self.assertNotEqual(env_result.returncode, 0)
        mutating = [c for c in self.calls() if any(word in c for word in (" stop ", " start ", "--cluster-reset", "delete node"))]
        self.assertEqual(mutating, [], "nothing may change before the operator confirms")
        self.assertEqual(list(self.backup_root.iterdir()), [])
        self.assertFalse(self.dropin_dir.exists())


if __name__ == "__main__":
    unittest.main()
