"""``python -m demo [--port N]`` — serve the demo on 127.0.0.1."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from demo import settings as settings_mod
from demo.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Symbiote demo (local only)")
    parser.add_argument("--port", type=int, default=settings_mod.load().port)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"\n  Symbiote demo → http://127.0.0.1:{args.port}\n")
    # The page keeps an event stream open, which never ends by itself: without
    # a deadline a stopped server waits for it forever, holding the database
    # and the open tab (which then never sees the new server and its token).
    uvicorn.run(create_app(port=args.port), host="127.0.0.1", port=args.port, log_level="warning",
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
