# Callback Site Test Naming

Use filename patterns that encode feature area and behavior:

- test_pages_<feature>.py: page rendering and page-level API tests.
- test_sd_prepare_<feature>.py: SD preparation and cloud-init option behavior.
- test_<integration_area>.py: endpoint or workflow integration tests.
- test_api_core_regression.py: temporary catch-all regression suite while older tests are being migrated.

Guidelines:

- Keep one dominant concern per file.
- Prefer explicit names over generic buckets.
- Add new tests to focused files first; only use the regression file when no focused module exists yet.
