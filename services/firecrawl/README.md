# Firecrawl (self-hosted) — local scrape backend for the firecrawl MCP

Serves the `firecrawl` MCP server (registered in `~/.claude.json` project scope `$HOME`:
`npx firecrawl-mcp` with `FIRECRAWL_API_URL=http://localhost:3002`, `FIRECRAWL_API_KEY=local`).

## Stack (5 containers, AGPL-3.0 upstream: mendableai/firecrawl)

| Container | Image | Role |
|---|---|---|
| firecrawl-api-1 | firecrawl-api (local build) | API on **127.0.0.1:3002** (loopback-only) |
| firecrawl-redis-1 | redis:alpine | queue/cache |
| firecrawl-rabbitmq-1 | rabbitmq:3-management | job queue |
| firecrawl-nuq-postgres-1 | firecrawl-nuq-postgres (local build) | job store |
| firecrawl-playwright-service-1 | firecrawl-playwright-service (local build) | rendering |

## Compose SOT: `docker-compose.yml` (this directory)

The original compose checkout (`~/projects/workspace/firecrawl/docker-compose.yaml`) was deleted;
`docker-compose.yml` here is the **reconstructed SOT** (from `docker inspect`, DIR-002 T2,
2026-06-13), validated by recreating the api container with it. The old "never compose-down"
warning relaxes to: **recreate via this compose file**. Caveats:

- **Local-build images**: `firecrawl-api` / `firecrawl-playwright-service` /
  `firecrawl-nuq-postgres` exist only in the local image store. If they are ever lost,
  rebuild requires re-cloning upstream firecrawl (~3.8GB).
- **Network is external**: `firecrawl_backend` predates this file — compose attaches, never
  manages it. From-scratch bootstrap: `docker network create firecrawl_backend` first.
- **Anonymous-volume data**: the LIVE rabbitmq + nuq-postgres containers store data in
  anonymous volumes. Recreating those two services starts with fresh named volumes
  (`firecrawl_rabbitmq-data` / `firecrawl_postgres-data`) unless data is migrated first.
  api / playwright-service / redis are stateless — recreate freely.
- **Secrets**: `cp .env.example .env && chmod 600 .env`, fill `POSTGRES_PASSWORD`.
  `.env` is gitignored (`**/.env`) — this repo is PUBLIC, never commit it.

## Operations

```bash
cd ~/.claude/services/firecrawl

# Status
sg docker -c "docker ps -a --filter name=firecrawl"

# Recreate/update ONE service (never drop --no-deps without reading the caveats above)
sg docker -c "docker compose --env-file .env up -d --no-deps api"

# Start the whole stack after a host reboot (restart=unless-stopped usually handles it)
sg docker -c "docker compose --env-file .env up -d"

# Verify (loopback is sandbox-blocked — run unsandboxed)
curl -s http://127.0.0.1:3002/                       # -> {"message":"Firecrawl API",...}
curl -s -X POST http://127.0.0.1:3002/v1/scrape -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local' -d '{"url":"https://example.com","formats":["markdown"]}'
```

## Incident history

- **2026-03-14 18:52**: all 5 containers stopped simultaneously (docker daemon/host shutdown,
  WSL). Restart policy was `no` → stack stayed dark ~3 months, surfacing as firecrawl MCP
  ECONNREFUSED on 127.0.0.1:3002 (observed 2026-06-12).
- **2026-06-12**: stack restarted (scaf DIR-001 T1); end-to-end scrape verified; restart policy
  set to `unless-stopped` on all 5 via `docker update` (survives daemon/host restarts now).
- **2026-06-13** (DIR-002 T2): api rebound `0.0.0.0:3002` → `127.0.0.1:3002` via reconstructed
  compose (`up -d --no-deps api`). First attempt failed: compose tried to recreate the live
  `firecrawl_backend` network (config-hash mismatch) — fixed by declaring it `external`.
  All gates passed post-swap (binding, /v1/scrape 200, crawl health, MCP).

## Known gaps

- **No healthcheck** on the api container (upstream image defines none; could be added to the
  compose service on a future recreation).
- **crawl4ai** (sibling service, `127.0.0.1:11235`) is docker-run managed, not in this compose.
