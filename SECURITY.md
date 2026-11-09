# Secrets and repository safety

## Before committing

Install the local staged-secret check with Docker running:

```bash
python3 -m venv .venv-security
.venv-security/bin/python -m pip install -r requirements-security.txt
.venv-security/bin/pre-commit install --hook-type pre-commit --hook-type pre-push
bash scripts/check_secrets.sh history
```

On Debian/Ubuntu, install the matching `python3-venv` package if virtual
environment creation reports that `ensurepip` is missing.

The pre-commit hook checks staged changes. The pre-push hook checks all locally
reachable history, preventing an old clone from reintroducing known credentials.
CI checks all history reachable from
the refs in its checkout, including pull-request commits, with the same digest-pinned
Gitleaks version and repository rules for Tailscale, Discord, and literal
credential assignments in configuration files such as Ansible variables,
Kubernetes manifests, Compose files, and environment files (including
commented-out assignments).
Findings are redacted; the scanner runs without network access.
The first run needs network access to pull the public scanner image.

Enable GitHub secret scanning and push protection where available, and require
the **Secret scan** check in the default branch's ruleset. Hooks can be bypassed,
and CI runs after a push: neither replaces server-side push protection. Pattern
scanning cannot prove that every arbitrary password or token is absent.

## Where credentials belong

- Keep Compose files tracked, but never put literal credentials in them. Use
  required environment interpolation (`${SERVICE_TOKEN:?Set SERVICE_TOKEN}`),
  or Compose file-backed secrets when the service supports reading a secret
  file. Do not silently fall back to a default password.
- Keep local `.env` files and secret files outside version control. Only commit
  example files with obvious placeholders. Compose `.env` substitution and
  environment variables are not a secret vault; container environments can be
  inspected by Docker administrators. Prefer a secret manager or file mounts
  for sensitive service credentials.
- Use Ansible Vault or an external secret manager for provisioning credentials,
  not plaintext inventory, group variables, playbooks, or cloud-init examples.
  Supply vault passwords outside the repository.
- The Pi Tailscale bootstrap requires `TAILSCALE_AUTHKEY` from the provisioning
  environment, with no embedded or fallback key. Obtain it from your private
  secret source without putting the value in shell history. Tailscale role
  tasks use `no_log` to avoid exposing it in Ansible output.
- Limit local credential files to their owner (`chmod 600`) and containing
  directories to `chmod 700`. Protect portal data volumes and backups: they can
  contain SSH keys, connection profiles, and provisioning credentials. Access
  to the Docker daemon also grants access to these secrets.
- Never print credentials in logs or share `docker compose config` output
  without inspecting and redacting it. Use short-lived, least-privilege tokens.
- Git ignores local environment files, private-key files, vault files, and
  history-rewrite replacement files. Docker excludes them from build context.
  Ignore rules do not untrack existing files or prevent `git add -f`.

## If a credential was committed

1. Revoke or rotate it at its issuer immediately, even if history was rewritten.
   Update the runtime secret source and inspect issuer access/audit logs.
2. Remove the literal value while preserving the Compose/deployment structure.
   Review every affected branch and tag; scan the rewritten history before
   publishing it. Do not paste leaked values into issues, PRs, or scanner logs.
3. Follow [GitHub's sensitive-data removal procedure](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
   Old commit URLs, pull-request refs, caches, forks, and other clones may still
   retain the data. Request GitHub Support cleanup where applicable.
4. Coordinate fresh clones with collaborators so old history is not pushed
   back. Keep replacement maps private and delete them when no longer needed.

Removing reachable history is not proof that a previously exposed token is safe.
