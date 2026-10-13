# Attaching to a running backend

How an external client (a script, a mod, the MCP server) finds a running COYPU Builder backend and
authenticates to it. The IPC protocol itself is in [ipc.md](ipc.md) (generated); the decision is ADR 0010.

## The session token

Every `serve` process creates its own token with `secrets.token_urlsafe(32)` (256 bits). There is no
`--token` option and no way to start the command-line server without a token. `session.hello` must carry the
token; a wrong or missing one gets `E_UNAUTHORIZED`, and the comparison is constant-time. The token is never
written to a command line, a log line or an error message, and never belongs in the repository.

## The discovery record

When it is listening, the backend writes one JSON file, atomically (temp file + rename), and removes it on
clean shutdown (SIGINT, SIGTERM, and `CTRL_BREAK` on Windows). A backend that is killed hard leaves its file
behind. Readers skip any record whose process is dead and delete it; a supervising client also deletes the
record of the backend it just killed.

| OS | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\COYPU Builder\runtime\backend-<pid>.json` |
| Linux | `$XDG_RUNTIME_DIR/coypu-builder/backend-<pid>.json`, else `~/.cache/coypu-builder/runtime/backend-<pid>.json` |

The directory is readable by the current user only. On Linux it is mode `0700` and the file `0600`. On
Windows the directory carries a protected (non-inherited) ACL that grants only the owner (OWNER RIGHTS, i.e.
the current user), SYSTEM and Administrators, and the file inherits it. A Windows directory that already
exists is checked for inherited entries and for any principal other than the current user, SYSTEM,
Administrators, OWNER RIGHTS and CREATOR OWNER (for example an explicit grant to Everyone). If it fails the
check it is repaired to a single explicit entry for the current user. If it cannot be repaired, or its ACL
cannot be read, the backend skips the discovery record, warns once on stderr and keeps serving: a
supervising client still gets the token on the startup line, but attach mode is unavailable. The file holds
no project path or project data.

```json
{
  "port": 51234,
  "pid": 4242,
  "protocol": "2",
  "backend_version": "0.1.0",
  "token": "<the session token>",
  "started_at": "2026-10-08T10:00:00.123456+00:00"
}
```

To attach: list `backend-*.json`, drop the ones whose `pid` is not running, pick the newest `started_at` (or
the one whose `port` you were told), connect to `ws://127.0.0.1:<port>` and send `session.hello` with the
record's `token`. Re-read the record on every reconnect: a restarted backend has a new port and token.
`coypu_builder.server.discovery.live_records()` does this in Python; the Godot client does it in
`client/ipc/discovery.gd`. Attach only to `127.0.0.1` (the only address the backend binds), build the URL
yourself from the record's port instead of reusing a caller-supplied one, and never send the token to any
other host. The Godot client accepts `--backend-url` only as `ws://127.0.0.1:<port>` (explicit port, no
userinfo, no path or query) and still connects to the URL it builds from the matching record.

## The startup line

A supervising parent (the Godot client) reads one JSON line from the backend's stdout:

```json
{"port": 51234, "token": "<the session token>", "pid": 4242, "protocol": "2"}
```

When stdout is a terminal the line has no `token`; it names the record instead
(`{"port": ..., "pid": ..., "protocol": "2", "discovery": "<path>"}`), so a manual run never shows the secret
on screen.

## Browsers are refused

The listener binds to `127.0.0.1` only, and a WebSocket handshake carrying any `Origin` header is answered
with HTTP 403 before a message is read. Non-browser clients send no `Origin` header.

## Client launch flags

`godot --path client -- --attach` attaches to the newest live backend; `--attach --backend-url
ws://127.0.0.1:<port>` picks the record on that port. A bare `godot --path client` spawns its own backend.
`tools\run_dev.ps1` starts a backend and then the client with `--attach`.
