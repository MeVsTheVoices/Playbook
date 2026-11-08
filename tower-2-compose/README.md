# Recovered Tower-2 Compose projects

These eight projects were recovered from the pre-cleanup repository with their
base and override files intact. They are separate from the root portal Compose
project and are **not** started by the root `docker compose up`.

They retain the original Unraid paths, networks, container names, devices,
privileges, and Docker-socket mounts. Review those requirements before running
them on another host. The Dockerbot project is an exported alternative to the
Discord Bot project, not an additional instance to run alongside it.

Literal credential fields are now required environment references. Store values
in an owner-only file outside the repository, or supply them from a secret
manager. Do not paste credentials into commands or commit resolved Compose
output. See the [credential-handling guide](../SECURITY.md).

| Project | Required private variables |
| --- | --- |
| `discord_bot`, `dockerbot` | `TOWER2_DISCORD_BOT_TOKEN` |
| `media` | `TOWER2_MEDIA_TS_AUTHKEY`, `TOWER2_MEDIA_ADD_AUTHKEY`, `TOWER2_MEDIA_VIEW_AUTHKEY` |
| `qbittorrent` | `TOWER2_MEDIA_ADD_AUTHKEY` |
| `romm` | `TOWER2_ROMM_DB_PASSWORD`, `TOWER2_ROMM_DB_ROOT_PASSWORD`, `TOWER2_ROMM_AUTH_SECRET_KEY`, `TOWER2_ROMM_SCREENSCRAPER_PASSWORD`, `TOWER2_ROMM_RETROACHIEVEMENTS_API_KEY` |
| `telegraf` | `TOWER2_TELEGRAF_INFLUX_TOKEN` |
| `zerobyte` | `TOWER2_ZEROBYTE_APP_SECRET` |
| `lancache` | None |

The three media authentication-key groups remain separate, and both RomM
database clients use the same database-user password variable. MariaDB's root
password is deliberately separate. Changing environment values does not rotate
passwords in an already initialized database; rotate those in the database too.

Validate a chosen project without printing credentials:

```bash
docker compose --env-file /private/tower-2.env \
  -f tower-2-compose/media/docker-compose.yml \
  -f tower-2-compose/media/docker-compose.override.yml config --quiet
```

Missing or empty required variables fail validation instead of falling back to
a built-in credential. Compose interpolation is not encryption: Docker admins
can inspect environments and labels, including TSDProxy authentication labels.
Keep Docker access restricted.

Recovery also normalizes exported boolean restart policies to `"no"`, makes
Telegraf's supplemental group a string, and drops Dockerbot's reserved
`com.docker.compose.*` runtime labels so Compose can validate the recovered files.
