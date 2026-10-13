"""Command-line entry point: `coypu-builder-backend <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
from datetime import UTC, datetime

from coypu_builder import PROTOCOL_VERSION, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coypu-builder-backend")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__} (protocol {PROTOCOL_VERSION})"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="parse a LandXML file and print an alignment summary")
    inspect.add_argument("path")
    inspect.add_argument(
        "--crs", default=None, help="project CRS (EPSG code, WKT or PROJ string); default: from file"
    )
    inspect.add_argument(
        "--spacing", type=float, default=100.0, help="frame table spacing in metres for the summary"
    )

    envelope = sub.add_parser(
        "envelope", help="write the corridor buffer polygon of a LandXML file as GeoJSON or Shapefile"
    )
    envelope.add_argument("path")
    envelope.add_argument("-o", "--output", required=True, help="output file; .geojson/.json or .shp")
    envelope.add_argument("--buffer", type=float, default=250.0, help="half-width in metres")
    envelope.add_argument(
        "--alignment", action="append", default=[], metavar="NAME", help="alignment name; repeatable"
    )
    envelope.add_argument("--from", dest="station_from", type=float, default=None, help="start station [m]")
    envelope.add_argument("--to", dest="station_to", type=float, default=None, help="end station [m]")
    envelope.add_argument("--cap", choices=("round", "flat"), default="round")
    envelope.add_argument(
        "--epsg", type=int, default=None, help="output EPSG; default 4326 (GeoJSON) or the project CRS (.shp)"
    )
    envelope.add_argument(
        "--crs", default=None, help="project CRS (EPSG code, WKT or PROJ string); default: from file"
    )

    envelope.add_argument("--force", action="store_true", help="overwrite existing output files")

    from coypu_builder.cli.cuzk import add_cuzk_parser

    add_cuzk_parser(sub)

    serve = sub.add_parser(
        "serve",
        help="run the IPC server (creates its own session token; see docs/protocol/attach.md)",
    )
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--project", default=None)
    return parser


def _discovery_warning(exc: BaseException) -> str:
    """One stderr line for a discovery failure; never contains the token."""
    return f"warning: discovery record skipped, attach mode unavailable ({exc})"


def _startup_line(port: int, pid: int, token: str, record_path: str) -> str:
    """The one line `serve` prints on stdout. The token is only included when stdout is not a terminal (a
    pipe to a supervising client), so a manual run never shows the secret on screen."""
    line: dict[str, object] = {"port": port, "pid": pid, "protocol": str(PROTOCOL_VERSION)}
    if sys.stdout.isatty():
        line["discovery"] = record_path
    else:
        line["token"] = token
    return json.dumps(line)


async def _serve(port: int) -> None:
    from coypu_builder.server import serve as ws_serve
    from coypu_builder.server.discovery import DiscoveryRecord, live_records, remove_record, write_record

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, request_stop)

    token = secrets.token_urlsafe(32)
    pid = os.getpid()
    server = await ws_serve(port=port, token=token)
    warned = False

    def discovery_failed(exc: BaseException) -> None:
        # A discovery failure (unwritable or unsafe runtime directory) only costs external attach: the
        # supervising client gets the token over the pipe, so the server keeps serving. Warn once.
        nonlocal warned
        if not warned:
            warned = True
            print(_discovery_warning(exc), file=sys.stderr, flush=True)

    try:
        try:
            live_records()  # sweeps the records of backends that died without cleaning up
        except OSError as exc:
            discovery_failed(exc)
        bound_port = server.sockets[0].getsockname()[1]
        record = DiscoveryRecord(
            port=bound_port,
            pid=pid,
            protocol=str(PROTOCOL_VERSION),
            backend_version=__version__,
            token=token,
            started_at=datetime.now(UTC).isoformat(),
        )
        try:
            record_path = str(write_record(record))
        except OSError as exc:
            record_path = ""
            discovery_failed(exc)
        print(_startup_line(bound_port, pid, token, record_path), flush=True)
        await stop.wait()
    finally:
        try:
            remove_record(pid)
        except OSError as exc:
            discovery_failed(exc)
        server.close()
        await server.wait_closed()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        from coypu_builder.cli.inspect import run_inspect

        return run_inspect(args.path, crs=args.crs, spacing=args.spacing)
    if args.command == "envelope":
        from coypu_builder.cli.envelope import run_envelope

        return run_envelope(
            args.path,
            args.output,
            args.buffer,
            args.alignment,
            args.station_from,
            args.station_to,
            args.cap,
            args.epsg,
            args.crs,
            args.force,
        )
    if args.command == "cuzk":
        from coypu_builder.cli.cuzk import run_cuzk

        return run_cuzk(args)
    if args.command == "serve":
        asyncio.run(_serve(args.port))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
