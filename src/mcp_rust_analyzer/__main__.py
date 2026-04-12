"""Entry point: python -m mcp_rust_analyzer"""

import asyncio
from .server import main as _main


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
