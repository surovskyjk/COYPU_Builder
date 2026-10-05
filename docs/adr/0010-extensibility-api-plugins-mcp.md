# ADR 0010 — Extensibility: one public API, plugins, and an MCP server

Status: decisions 1–3 accepted (2026-10-06); decisions 4–6 proposed, to be confirmed by the user when their
tasks are specified.

## Context

The user wants mods and AI tooling (MCP) to be easy to add later. Both need the same things: a stable API to
call, a way to attach to a running app, and a security model, because anything that can attach can also
import files, write exports and read project data.

Most of that already exists. The IPC protocol (ADR 0003) is a typed method registry with generated
documentation, bound to `127.0.0.1` and checked by a session token. Its weaknesses are cheap to fix now and
expensive later (F28):

- the client's token is not cryptographically random;
- an empty token disables the check;
- the token travels on the backend's command line, where other local processes can read it;
- browser `Origin` headers are not rejected, so a web page could try the local port.

The extensions themselves are not worth building yet. An MCP server is only useful once there is something
to drive (imports, envelope export, terrain), and client mods need the M4 UI shell's extension points.

## Decision

1. **The IPC protocol is the one public API.** Mods, scripts and the MCP server use the same methods the
   Godot client uses; there is no second API. Every method in `METHODS` carries a stability tier —
   `internal`, `experimental` or `stable` — shown in the generated docs. A breaking change to a `stable`
   method needs a protocol version bump and a deprecation period.
2. **Attaching is explicit and authenticated.** A running backend writes a discovery record (port, process
   id, protocol version, token) to a per-user runtime file only that user can read. External clients attach
   with that token. The token is 256 bits from a cryptographic source, always required (an empty token is
   accepted only by the test harness), compared in constant time, never logged, never written into the
   repository, and never passed on a command line — the client hands it to the backend through the
   environment or stdin.
3. **The listener stays local and refuses browsers.** It binds to loopback only and rejects WebSocket
   handshakes that carry a browser `Origin` header. Sessions get scopes later (read, edit, import,
   file-system write), so an AI client can be granted read-only access. Every method that writes a file takes
   an absolute path, refuses a path inside the repository, and never overwrites an existing file unless the
   caller sets `overwrite`. `alignment.envelope` (T-138) is the first such method. Without these rules, any
   client holding the token could replace goldens or tool configuration such as `.claude/settings.json`.
4. *Proposed:* **Backend plugins are Python entry points.** Groups `coypu_builder.readers`,
   `coypu_builder.providers` and `coypu_builder.methods` are discovered at startup. Plugins add readers and
   providers behind the M3/M5 interfaces, and add methods only under a namespaced prefix
   (`x.<plugin>.<name>`). Plugins are trusted code the user installs. `session.hello` lists them, and each
   can be disabled.
5. *Proposed:* **Client mods are Godot resource packs** loaded from a per-user mods folder at startup. They
   register panels, tools and layers through a small `ModApi` autoload. Each mod is trusted code with
   explicit opt-in. This is Phase 2, once the UI shell's extension points exist.
6. *Proposed:* **The MCP server is a separate stdio process,** `coypu-builder-mcp`. It attaches to a running
   app through the discovery record, or starts a headless backend for batch work. It exposes `stable`
   methods as MCP tools whose JSON schemas are generated from the registry's msgspec types, plus resources
   (project summary, alignments, sources) and a screenshot tool built on the T-125 capture harness. It is
   read-only by default; tools that import or write files need the user's opt-in.

## Consequences

- T-126 implements 1–3 before anything external can attach.
- T-130 makes readers and providers registrable, so the first plugin hook arrives with M3 at almost no extra
  cost.
- The MCP server (T-146) lands in M4, when the import, export and capture methods it would expose exist.
- Client mods and method plugins are Phase 2.
