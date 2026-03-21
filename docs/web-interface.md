# Web Interface

The web interface provides browser-based access to Claude Code, using GardenFS's Supabase auth for authentication. It's designed for the frontend to live on Vercel (or any static host) while the API runs on the Fly machine alongside the Telegram bot.

## Architecture

```
Browser (Vercel)        GardenFS Server (:3000)     Bot API (:8234)       Supabase
  |                         |                           |                    |
  |-- supabase.signIn() ---|---------------------------|-------------------->|
  |<-- JWT ----------------|---------------------------|<--------------------|
  |                         |                           |                    |
  |-- POST /web/chat ------>|                           |                    |
  |   (Bearer JWT)          |-- proxy /web/chat ------->|                    |
  |                         |                           |-- validate JWT --->|
  |                         |                           |<-- user info ------|
  |                         |                           |                    |
  |                         |                           |-- run_command()    |
  |<-- SSE stream ----------|<-- SSE stream ------------|                    |
```

### Deployment model

- **Frontend (SPA)**: Hosted on Vercel, Netlify, or any static host. Points at the Fly machine URL via the `?api=` query parameter.
- **GardenFS server (port 3000)**: Already exposed by Fly. Proxies `/web/*` requests to the bot's API server on port 8234 inside the container. No new ports needed.
- **Bot API (port 8234)**: Internal only. Handles Supabase JWT validation, Claude integration, and SSE streaming. Not directly exposed to the internet.

The proxy is a simple `http.request` pipe in `server.js` that forwards the request (including `Authorization` header) and streams the response back. This means SSE events flow through without buffering.

### Key design decisions

- **SSE (not WebSocket)** for streaming — the interaction is request-response with a long-lived response stream. SSE fits naturally and avoids the complexity of bidirectional state management.
- **Proxy through port 3000** — avoids opening a second port on Fly. The GardenFS server already handles CORS and is the public-facing endpoint.
- **Supabase token validation via REST API** — mirrors the GardenFS `server.js` pattern (`auth.getUser(token)`). No Supabase Python SDK dependency needed, just one `httpx` GET call.
- **Deterministic user ID mapping** — Supabase UUIDs are converted to 64-bit integers with bit 62 set high, avoiding collision with Telegram user IDs (typically < 2^40). This means all existing infrastructure (rate limiter, session manager, storage) works without changes.
- **Zero changes to existing bot code paths** — the web handler calls `ClaudeIntegration.run_command()` directly, the same interface the Telegram orchestrator uses. The `on_stream` callback pushes events to an `asyncio.Queue` which the SSE generator reads from.

## Files

| File | Purpose |
|------|---------|
| `src/api/web_auth.py` | `SupabaseAuthValidator` — validates JWTs via Supabase REST API with 5-minute cache |
| `src/api/web_handler.py` | `WebHandler` — bridges `ChatRequest` → `ClaudeIntegration.run_command()` → SSE events |
| `src/api/web_routes.py` | FastAPI route registration (`/web`, `/web/chat`, `/web/new`, `/web/sessions`, `/web/status`) |
| `src/api/static/index.html` | Self-contained SPA (HTML/CSS/JS, no build step) with Supabase JS for auth |
| `src/config/settings.py` | New settings: `enable_web_interface`, `supabase_url`, `supabase_anon_key`, `api_server_host` |
| `src/config/features.py` | New feature flag: `web_interface_enabled` |
| `src/api/server.py` | Modified to accept auth validator, add CORS and web routes when enabled |
| `src/main.py` | Modified to create `SupabaseAuthValidator` and pass it through to the API server |

**GardenFS side:**

| File | Purpose |
|------|---------|
| `server.js` | Added `proxyToBotAPI()` — forwards `/web/*` requests to bot API on port 8234 |
| `lib/claude-bot.js` | Passes `ENABLE_WEB_INTERFACE`, `SUPABASE_URL`, `SUPABASE_ANON_KEY` to bot env |

## Configuration

When deployed via GardenFS on Fly, the web interface env vars are set automatically by `claude-bot.js`. No manual configuration is needed on the bot side.

For standalone bot deployment (without GardenFS), set these env vars:

### Required environment variables

```env
ENABLE_API_SERVER=true
ENABLE_WEB_INTERFACE=true
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIs...   # GardenFS Supabase anon key
```

### Optional

```env
SUPABASE_URL=https://fmkytgctlrjdvpiusuhv.supabase.co   # default
API_SERVER_HOST=127.0.0.1                                  # default (behind GardenFS proxy)
API_SERVER_PORT=8234                                       # default when launched by GardenFS
```

### Frontend hosting

The SPA (`index.html`) can be served from:

1. **The bot directly** — `GET /web` serves it from the FastAPI server (or proxied through GardenFS at `https://<machine>.fly.dev/web`)
2. **A separate static host** (Vercel, Netlify, etc.) — deploy `src/api/static/index.html` and pass the Fly machine URL as a query parameter:

```
https://your-app.vercel.app/?api=https://gardenfs.fly.dev
```

The SPA reads `?api=` to know where to send requests. If omitted, it defaults to `window.location.origin` (same-origin mode).

When hosted on Vercel, each garden's Fly machine URL needs the `fly-force-instance-id` header for routing. The GardenFS proxy on port 3000 is already the target — requests hit `https://gardenfs.fly.dev/web/chat` with the instance header, the GardenFS server proxies to `localhost:8234`, and the bot handles it.

### Supabase redirect URL

The Google OAuth redirect URL must be registered in the Supabase project's auth settings (Dashboard > Authentication > URL Configuration > Redirect URLs). Add the frontend URL, e.g.:
- `http://localhost:8080/web` for local development
- `https://your-app.vercel.app` for production

## SSE Protocol

The `/web/chat` endpoint returns `text/event-stream`. Each event is a JSON object:

```
event: tool
data: {"name": "Read", "detail": "src/main.py"}

event: reasoning
data: {"content": "Let me look at the entry point..."}

event: delta
data: {"content": "Here is"}

event: delta
data: {"content": " the analysis..."}

event: done
data: {"content": "Full response text...", "session_id": "abc123", "cost": 0.05, "duration_ms": 3200, "num_turns": 3, "tools_used": ["Read", "Grep"]}

event: error
data: {"content": "Rate limit exceeded. Try again in 30 seconds."}
```

| Event | When | Contains |
|-------|------|----------|
| `tool` | Claude invokes a tool | `name`, `detail` (file path, command, pattern) |
| `reasoning` | Claude emits assistant text | `content` (first line, max 120 chars) |
| `delta` | Token-by-token text streaming | `content` (text chunk) |
| `done` | Request complete | `content`, `session_id`, `cost`, `duration_ms`, `num_turns`, `tools_used` |
| `error` | Something went wrong | `content` (error message) |

## API Reference

All endpoints except `GET /web` require authentication via the `Authorization` header with a Supabase JWT.

When calling through GardenFS on Fly, include the `fly-force-instance-id` header to route to the correct machine.

### Common headers

```
Authorization: Bearer <supabase_jwt>       # Required (except GET /web)
Content-Type: application/json             # Required for POST bodies
fly-force-instance-id: <machine_id>        # Required when routing through Fly
```

### Common error responses

All endpoints return JSON error bodies:

```json
{"detail": "Missing Bearer token"}         // 401 — no Authorization header
{"detail": "Invalid or expired token"}     // 401 — Supabase rejected the JWT
{"detail": "Not Found"}                    // 404 — unknown route
{"detail": "Internal Server Error"}        // 500 — server error
```

---

### `GET /web`

Serves the static SPA frontend. No authentication required.

**Response:** `text/html` — the `index.html` file.

---

### `POST /web/chat`

Send a message to Claude and receive a streaming response via Server-Sent Events.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | The user's message to Claude |
| `session_id` | string | no | Session ID to resume. If omitted, auto-resumes the most recent session for this user + working directory. |
| `working_directory` | string | no | Override the working directory for this request. Defaults to the user's current directory or `APPROVED_DIRECTORY`. |
| `force_new` | boolean | no | If `true`, start a fresh session instead of resuming. Default `false`. |

**Example request:**

```bash
curl -N -X POST https://gardenfs.fly.dev/web/chat \
  -H "Authorization: Bearer eyJhbG..." \
  -H "Content-Type: application/json" \
  -H "fly-force-instance-id: 6e82de1a..." \
  -d '{"message": "list the files in src/"}'
```

**Response:** `text/event-stream` (SSE). The connection stays open until Claude finishes. Events are emitted in this order:

1. Zero or more `tool` / `reasoning` / `delta` events (interleaved, as Claude works)
2. Exactly one `done` or `error` event (terminates the stream)

**SSE event types:**

#### `tool`

Emitted when Claude invokes a tool (Read, Write, Bash, etc.).

```
event: tool
data: {"name": "Read", "detail": "src/main.py"}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Tool name (e.g. `Read`, `Bash`, `Grep`, `Edit`) |
| `detail` | string | Summary of the tool input — file path, command, or pattern |

#### `reasoning`

Emitted when Claude produces assistant text (thinking/commentary) during execution.

```
event: reasoning
data: {"content": "Let me check the configuration..."}
```

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | First line of assistant text, max 120 chars |

#### `delta`

Token-by-token text streaming of Claude's response as it's generated.

```
event: delta
data: {"content": "Here is "}
```

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | Text chunk |

Concatenate all `delta` contents to build the full response incrementally. The `done` event also contains the complete response text.

#### `done`

Emitted exactly once when the request completes successfully. Terminates the stream.

```
event: done
data: {"content": "Here is the analysis...", "session_id": "abc-123", "cost": 0.0512, "duration_ms": 4200, "num_turns": 3, "tools_used": ["Read", "Grep"]}
```

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | Complete response text |
| `session_id` | string | Session ID (pass back in next request to continue the conversation) |
| `cost` | number | API cost in USD for this request |
| `duration_ms` | number | Wall-clock time in milliseconds |
| `num_turns` | number | Number of Claude turns (tool use cycles) |
| `tools_used` | string[] | List of tool names used during this request |

#### `error`

Emitted if something goes wrong. Terminates the stream.

```
event: error
data: {"content": "Rate limit exceeded. Try again in 30 seconds."}
```

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | Error message |

**Concurrency:** Only one `POST /web/chat` request can be active per user at a time. If a second request arrives while one is in progress, it returns immediately:

```
event: error
data: {"content": "A request is already in progress. Please wait."}
```

---

### `POST /web/new`

Clear the current session, so the next `POST /web/chat` starts a fresh conversation. Equivalent to the `/new` Telegram command.

**Request body:** None (empty or `{}`).

**Example:**

```bash
curl -X POST https://gardenfs.fly.dev/web/new \
  -H "Authorization: Bearer eyJhbG..." \
  -H "fly-force-instance-id: 6e82de1a..."
```

**Response:**

```json
{"status": "ok"}
```

---

### `GET /web/sessions`

List all Claude sessions for the authenticated user.

**Example:**

```bash
curl https://gardenfs.fly.dev/web/sessions \
  -H "Authorization: Bearer eyJhbG..." \
  -H "fly-force-instance-id: 6e82de1a..."
```

**Response:**

```json
[
  {
    "session_id": "abc-123-def",
    "project_path": "/data/garden-server",
    "created_at": "2026-03-21T10:30:00+00:00",
    "last_used": "2026-03-21T11:15:00+00:00",
    "total_cost": 0.42,
    "message_count": 12,
    "tools_used": ["Read", "Edit", "Bash"],
    "expired": false
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session ID |
| `project_path` | string | Working directory for this session |
| `created_at` | string | ISO 8601 timestamp |
| `last_used` | string | ISO 8601 timestamp of last activity |
| `total_cost` | number | Cumulative cost in USD |
| `message_count` | number | Number of messages in this session |
| `tools_used` | string[] | All tools used across the session |
| `expired` | boolean | Whether the session has timed out |

---

### `GET /web/status`

Get the current user's status and active session info.

**Example:**

```bash
curl https://gardenfs.fly.dev/web/status \
  -H "Authorization: Bearer eyJhbG..." \
  -H "fly-force-instance-id: 6e82de1a..."
```

**Response:**

```json
{
  "user": {
    "email": "user@example.com",
    "name": "Jane Doe"
  },
  "session_id": "abc-123-def",
  "working_directory": "/data/garden-server"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `user.email` | string | User's email from Supabase |
| `user.name` | string\|null | User's display name from Google |
| `session_id` | string\|null | Current active session ID, or null if no session |
| `working_directory` | string | Current working directory |

## Auth Flow

1. Browser loads `/web` — static SPA served by FastAPI
2. SPA initializes Supabase JS client with the GardenFS project URL and anon key
3. User clicks "Sign in with Google" → `supabase.auth.signInWithOAuth({ provider: 'google' })`
4. After OAuth redirect, Supabase JS exchanges the code for a session (PKCE flow)
5. SPA sends `POST /web/chat` with `Authorization: Bearer <jwt>`
6. Server calls `GET {supabase_url}/auth/v1/user` with the JWT to validate (cached 5 min)
7. Supabase returns user info (id, email, metadata) or 401
8. Server converts Supabase UUID → deterministic int user_id, then calls `claude_integration.run_command()`

Token refresh is handled automatically by Supabase JS. The server just validates whatever token it receives.

## Session Management

Per-user state (current session ID, working directory) is held in memory on the server, keyed by the derived integer user_id. This is volatile (lost on restart) but matches the Telegram behavior (`context.user_data`).

Claude sessions themselves are persisted in SQLite via `SessionManager`, so a server restart loses the in-memory pointer but the sessions can still be auto-resumed on the next request.

Concurrent requests from the same user are serialized with a per-user `asyncio.Lock`. If a request arrives while another is in progress, it returns an error event immediately rather than queueing.

## Reuse of Existing Infrastructure

The web interface reuses these components directly, with zero modifications:

- **ClaudeIntegration** (`src/claude/facade.py`) — same `run_command()` call
- **ClaudeSDKManager** (`src/claude/sdk_integration.py`) — same streaming callback
- **SessionManager** — same auto-resume, same SQLite persistence
- **Storage** — same `save_claude_interaction()` for history
- **RateLimiter** — same token bucket, keyed by user_id
- **SecurityValidator** — same path boundary checks
- **EventBus** — same event system (web could publish/subscribe events in the future)
