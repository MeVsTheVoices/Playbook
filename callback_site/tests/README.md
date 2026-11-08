# Callback Site Test Naming

Use filename patterns that encode feature area and behavior:

- test_<feature>.py: one file per feature area (callbacks, images, monitor, node provisioning, node inventory, SD card, settings, SSH keys, terminal checks, repo config).
- test_k3s_lightweight.py, test_tailscale_policy.py: board/role rules and the Tailscale policy builder.
- test_support.py: shared base class and `patch_in`, which patches a name in every module that imported it. Use `page_text()` for assertions on text that now lives in linked static files.

Guidelines:

- Keep one dominant concern per file.
- Prefer explicit names over generic buckets.
- Add new tests to the focused file for their feature.
- `BaseCallbackSiteTest` mocks automatic callback provisioning so ordinary callback fixtures never launch Ansible. Tests of scheduling should explicitly patch `_start_node_provision_job`; worker lifecycle tests call the worker with mocked inventory and execution. Do not run real checks against repository inventory from unit tests.
