#!/usr/bin/env python3
"""Entrypoint for the film-matinee MCP reader."""

from film_matinee_mcp import mcp


if __name__ == "__main__":
    mcp.run("stdio")
