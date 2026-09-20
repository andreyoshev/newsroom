# newsroom

Self-hosted bridge from WhatsApp groups to a Telegram digest, with Claude in
the middle doing the summarising.

Each person signs up, links **their own** WhatsApp by QR, picks which groups
make up a **feed**, and connects that feed to Claude. A scheduled Claude
routine reads the last day of messages and posts a summary to the Telegram
chat of their choice.

```
you ──sign up──▶ web (dashboard)
                  ├─ link WhatsApp by QR ──▶ your own wacli store (isolated)
                  ├─ feed: groups + local names + a Telegram bot/chat
                  └─ /mcp (OAuth) ──▶ Claude routine ──▶ Telegram

wacli-supervisor ──▶ one `wacli sync --follow` per linked account
```

Multi-account for real: every user gets their own WhatsApp store under
`/wacli-state/users/<id>/`, and the supervisor runs a separate sync for each.

## What it is made of

Two services in docker-compose:

| Service | What it does |
|---|---|
| **web** | FastAPI: the dashboard (Jinja2 + CSS), an OAuth 2.1 authorization server, and an MCP server mounted at `/mcp`. Login and sessions, WhatsApp linking by QR over SSE, feed CRUD. |
| **wacli-supervisor** | Reads app.db and, for every `linked` WhatsApp account, runs its own `wacli sync --follow` — restarting with backoff, pausing during re-linking. |

State is a single SQLite `app.db` on the shared `wacli-state` volume (WAL, read
by both services). Sources sit behind a `SourceProvider` abstraction — WhatsApp
today; adding Telegram or Viber as a *source* means registering another
provider, not changing the schema.

The shared code is the `shared/newsroom_shared` package, installed into both
images. WhatsApp itself is handled by [wacli](https://github.com/openclaw/wacli).

## Requirements

- A machine with Docker and the Compose v2 plugin.
- A way to serve it over https: Cloudflare Tunnel, or any reverse proxy that
  terminates TLS and forwards `Host` and `X-Forwarded-Proto`. Claude will not
  talk to an MCP server over plain http, and OAuth needs a stable public URL.
- A phone with WhatsApp, to link as a companion device.
- A Telegram bot token from [@BotFather](https://t.me/botfather) and the id of
  the chat to post into, from [@userinfobot](https://t.me/userinfobot).

## Getting started

```bash
git clone https://github.com/andreyoshev/newsroom && cd newsroom
./deploy.sh
```

That builds both images, starts the stack and waits for `/healthz`. By default
the web service is bound to `127.0.0.1:18080`, which is what you want with a
tunnel or a proxy on the same host; to expose it directly instead, set
`NEWSROOM_BIND=0.0.0.0:8080` in the environment of `docker compose` (or of
`deploy.sh`).

Then point your tunnel or proxy at that address and open the service in a
browser. **There is no configuration file and no `.env`.** Until it has been
set up, every path redirects to a one-time setup page, which asks for:

- the admin account (email and password — the first account is the admin);
- the public address, prefilled from how you got there;
- who may sign up: invite-only, closed, or open.

Everything it collects goes into `app.db` and can be changed afterwards under
**Settings**, which is also where you regenerate the invite code, set a token
for the health endpoints, manage accounts, and tune the sync limits.

One thing worth pinning down early: the **public address** is also the OAuth
issuer. Changing it later invalidates connectors already added to Claude, and
they have to be authorized again.

## Using it

1. **Link WhatsApp.** Dashboard → *Connect* → scan the QR from your phone under
   *Settings → Linked devices*. The supervisor starts syncing on its own.
2. **Build a feed.** *Feeds → New feed*: a name, a Telegram bot token and a
   chat id. Then tick the source groups and give them local names. *Send test*
   checks the Telegram side.
3. **Connect Claude.** The feed page shows the connector URL — add it in Claude
   and authorize; see below.

## Connecting Claude

MCP is served at `<public address>/mcp` and protected by **OAuth 2.1** — there
are no tokens to copy around.

**Claude Code:**
```bash
claude mcp add --transport http newsroom https://news.example.com/mcp
```

**claude.ai:** add a Custom Connector with the same URL.

Either way, the first call opens a browser: you sign in to the dashboard, pick
which feed to grant access to, and the token is issued. Refresh tokens last 90
days, so a scheduled routine keeps working without re-authorizing.

One grant is one feed. To use a second feed in Claude, add the connector again
and pick the other feed.

**A routine** (`/schedule`) might run at `0 9 * * *` with a prompt along the
lines of:

> Call `fetch_recent_messages` for the last 24 hours, write a short digest
> grouped by chat, and send it with `publish_to_telegram`.

### MCP tools

| Tool | What it does |
|---|---|
| `list_feed_sources` | The feed's source chats (chat_jid plus local name). |
| `fetch_recent_messages(since_hours=24)` | Messages from those chats over that window, from the account's own store, without reactions or deleted messages, oldest first. |
| `publish_to_telegram(text)` | Posts HTML to the feed's Telegram destination, split into chunks of at most 4096 characters. |

## Running it

```bash
docker compose logs -f web wacli-supervisor   # logs
docker compose up -d --build                  # rebuild after changes
./deploy.sh                                   # idempotent redeploy
```

`GET /healthz` and `GET /health` are public liveness checks. The deeper ones —
`/health/integrations`, `/health/whatsapp`, `/health/whatsapp/<connection_id>` —
report whether each WhatsApp session is still authenticated and actually
syncing, and answer 503 when it is not, which is what an uptime monitor wants.
They sit behind the health token from Settings; the per-connection id is shown
to its owner on the Providers page as *Monitor ID*.

## Security notes

- Passwords are hashed with argon2. Sessions are server side, in an httponly
  cookie that is marked `Secure` whenever the browser arrives over https —
  which behind a proxy means it has to forward `X-Forwarded-Proto`.
- MCP is behind OAuth 2.1 (PKCE, refresh rotation) and each token is scoped to
  a single feed.
- Each account's WhatsApp store is isolated on disk, and messages are read
  read-only.
- `tg_bot_token` is stored in `app.db` in the clear. Keep the volume to
  yourself; if you want it encrypted, `shared/newsroom_shared/db.py` is the one
  place to add it.

## License

MIT — see [LICENSE](LICENSE).
