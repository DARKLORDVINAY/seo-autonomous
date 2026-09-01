"""Compatibility launcher; avoids shadowing the installed `mcp` Python package."""
from seo_mcp.server import main

if __name__ == "__main__":
    main()
