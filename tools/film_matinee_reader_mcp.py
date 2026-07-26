#!/usr/bin/env python3
"""Entrypoint for the film-matinee MCP reader."""

import os
import sys

from film_matinee_mcp import mcp
from film_matinee_cache import cleanup_expired


if __name__ == "__main__":
    try:
        max_age_hours = float(os.environ.get("FILM_MATINEE_CACHE_TTL_HOURS", "24"))
        cleanup_expired(max_age_hours=max_age_hours)
    except Exception as exc:
        print(f"[film-matinee] startup cache cleanup skipped: {exc}", file=sys.stderr)
    mcp.run("stdio")
