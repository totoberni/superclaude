# Discovery egress stack (D3)

VPN-masked egress for the DISCOVERY scraper only. Applications never ride this path.

## D3 hard conditions (all four required, locked 2026-07-01)

1. NordVPN IP masking: the browser container egresses only through gluetun.
2. Always logged-out / incognito: fresh anonymous contexts; never authenticate.
3. Low frequency: small daily slice, only on ToS-restricted vendors; API-first everywhere else.
4. Never tied to the owner's identity or account.

## Layout

- `docker-compose.yml`: gluetun (NordVPN, WireGuard/NordLynx, pinned v3.41.0) + Playwright server container inside gluetun's network namespace. Fail-closed: VPN down = browser has no egress; the rest of toto (Tailscale, ssh, ntfy, SDK) is untouched.
- `.env.example`: copy to `.env` (chmod 600), fill with the NordVPN NordLynx WireGuard key (see the header in that file for the one-time extraction). Never committed, never synced (WT-2).

## Why WireGuard, not OpenVPN (research 2026-07-02)

One static key vs OpenVPN's recurring AUTH_FAILED foot-guns; lower CPU on the headless
box; gluetun auto-resolves the endpoint + peer pubkey for the built-in nordvpn provider.
Nord did NOT remove OpenVPN (that was Mullvad, Jan 2026); the manual-setup entry just
relocated behind an email-code gate. OpenVPN service creds remain a documented fallback.

## Bring-up

```
cd ~/automations/discovery
cp .env.example .env && chmod 600 .env   # then fill credentials
docker compose up -d
docker compose ps
```

## Verify the egress split (empirical, every bring-up)

```
docker compose exec scraper-browser sh -c 'wget -qO- https://ifconfig.me'  # expect VPN IP
curl -s https://ifconfig.me                                                # expect home IP
tailscale status | head -3                                                 # tailnet unaffected
```

## Client connection

Host-side agents connect to the Playwright server at `ws://127.0.0.1:9222/` (bound to
loopback only). Keep the host-side `playwright` Python package pinned to the same minor
version as the container image (1.56.x) or use `connect_over_cdp` to loosen coupling.

## Notes (from the R-WT-8 research campaign, 2026-07-02)

- Official nordvpn-linux client REJECTED: no per-app split tunnel, allowlist regressions,
  Tailscale coexistence requires disabling its killswitch (breaks fail-closed).
- gluetun + OpenVPN service credentials is the NordVPN-documented third-party setup (ToS-clean).
- Startup race: `depends_on: service_healthy` gates the browser on a healthy tunnel.
- If DNS-over-TLS flaps (known gluetun issue), set `DOT: "off"` (queries still exit via tunnel).
- Restart the browser container between discovery batches (Chromium memory creep).
