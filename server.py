"""Console entry point for the EPANET 2.2 MCP server."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from epanet_mcp.server import main

if __name__ == "__main__":
    main()
