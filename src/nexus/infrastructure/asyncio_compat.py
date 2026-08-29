"""Platform event-loop compatibility required by async PostgreSQL drivers."""

from __future__ import annotations

import asyncio
import sys


def configure_asyncio_policy() -> None:
    """Use the Windows selector loop required by psycopg async connections."""

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
