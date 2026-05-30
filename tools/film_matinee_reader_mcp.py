#!/usr/bin/env python3
"""Compatibility entrypoint for the film-matinee MCP reader."""

from cinema_reader_mcp import mcp


if __name__ == "__main__":
    mcp.run("stdio")
