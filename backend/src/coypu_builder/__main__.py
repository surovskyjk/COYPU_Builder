"""Command-line entry point: `coypu-builder-backend <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

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

    serve = sub.add_parser("serve", help="run the IPC server for the Godot client")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--token", default="")
    serve.add_argument("--project", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        from coypu_builder.cli.inspect import run_inspect

        return run_inspect(args.path, crs=args.crs, spacing=args.spacing)
    if args.command == "serve":
        from coypu_builder.server import serve as ws_serve

        async def _run() -> None:
            server = await ws_serve(port=args.port, token=args.token)
            port = server.sockets[0].getsockname()[1]
            print(json.dumps({"port": port}), flush=True)
            async with server:
                await server.serve_forever()

        asyncio.run(_run())
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
