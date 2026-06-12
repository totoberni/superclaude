# Firecrawl (self-hosted) — local scrape backend for the firecrawl MCP

Serves the `firecrawl` MCP server (registered in `~/.claude.json` project scope `$HOME`:
`npx firecrawl-mcp` with `FIRECRAWL_API_URL=http://localhost:3002`, `FIRECRAWL_API_KEY=local`).

## Stack (5 containers, AGPL-3.0 upstream: mendableai/firecrawl)

| Container | Image | Role |
|---|---|---|
| firecrawl-api-1 | firecrawl-api (local build) | API on **0.0.0.0:3002** |
| firecrawl-redis-1 | redis:alpine | queue/cache |
| firecrawl-rabbitmq-1 | rabbitmq:3-management | job queue |
| firecrawl-nuq-postgres-1 | firecrawl-nuq-postgres (local build) | job store |
| firecrawl-playwright-service-1 | firecrawl-playwright-service (local build) | rendering |

## ⚠ Compose source is GONE

The original compose project lived at `~/projects/workspace/firecrawl/docker-compose.yaml` — that
checkout was deleted (containers' compose labels still point there). The stack survives as
**docker-managed containers + locally built images only**. No bind mounts (named volumes only:
rabbitmq + postgres data), so restarts are safe without the source tree.

**Do NOT `docker compose down`** this stack — without the compose file + build contexts,
recreating containers requires re-cloning upstream firecrawl and rebuilding (~3.8GB of images).

## Operations

```bash
# Status
sg docker -c "docker ps -a --filter name=firecrawl"

# Start (dependency order; api last)
sg docker -c "docker start firecrawl-redis-1 firecrawl-rabbitmq-1 firecrawl-nuq-postgres-1 firecrawl-playwright-service-1 && sleep 5 && docker start firecrawl-api-1"

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

## Known gaps

- **Port binding**: api binds `0.0.0.0:3002` (LAN-exposed; WSL2 NAT limits practical exposure to
  the Windows host). crawl4ai by contrast binds `127.0.0.1` only. Fixing requires container
  recreation (compose source gone) — accepted risk, revisit if the stack is ever rebuilt.
- **No healthcheck** on the api container (compose source gone; `docker update` can't add one).
- Long-term: if firecrawl remains load-bearing, re-clone upstream pinned, rebuild under
  `~/.claude/services/firecrawl/` with localhost-only binding + healthchecks + compose SOT.
