"""Provisioning job lifecycle failures and retained progress."""

from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

from services import provision_jobs as jobs


class ProvisionJobTests(BaseCallbackSiteTest):
    def setUp(self):
        super().setUp()
        self.job_id = "lifecycle-test"
        jobs.PROVISION_JOBS[self.job_id] = {"status": "queued", "results": []}
        self.addCleanup(jobs.PROVISION_JOBS.pop, self.job_id, None)

    def test_missing_node_is_failure_not_empty_success(self):
        with patch.object(jobs, "_node_ops_inventory_view", return_value=[]):
            jobs._run_node_provision_job(self.job_id, ["missing"], "check", True)
        job = jobs.PROVISION_JOBS[self.job_id]
        self.assertEqual(job["status"], "failed")
        self.assertIn("no longer in inventory", job["error"])

    def test_empty_targets_and_profiles_fail_explicitly(self):
        for targets, profiles in (([], ["baseline"]), (["node"], [])):
            with patch.object(jobs, "_node_ops_inventory_view", return_value=[{"name": "node"}]), \
                 patch.object(jobs, "_profiles_to_run", return_value=profiles):
                jobs._run_node_provision_job(self.job_id, targets, "check", True)
            self.assertEqual(jobs.PROVISION_JOBS[self.job_id]["status"], "failed")

    def test_failure_keeps_completed_node_results(self):
        result = {"node": "first", "results": [{"profile_id": "baseline", "status": "ok"}]}
        with patch.object(jobs, "_node_ops_inventory_view", return_value=[{"name": "first"}]), \
             patch.object(jobs, "_profiles_to_run", return_value=["baseline"]), \
             patch.object(jobs, "_apply_node_provisioning", return_value=result):
            jobs._run_node_provision_job(self.job_id, ["first", "removed"], "check", True)
        self.assertEqual(jobs.PROVISION_JOBS[self.job_id]["status"], "failed")
        self.assertEqual(jobs.PROVISION_JOBS[self.job_id]["results"], [result])

    def test_enqueue_runs_worker_as_daemon_and_exposes_queued_status(self):
        with patch.object(jobs.threading, "Thread") as thread:
            job_id = jobs._start_node_provision_job(["node"], "check", True, ["baseline"])
        self.addCleanup(jobs.PROVISION_JOBS.pop, job_id, None)
        self.assertEqual(jobs.PROVISION_JOBS[job_id]["status"], "queued")
        self.assertTrue(thread.call_args.kwargs["daemon"])
        self.assertIs(thread.call_args.kwargs["target"], jobs._run_node_provision_job)
        thread.return_value.start.assert_called_once()
