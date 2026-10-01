import argparse
from pathlib import Path

import uvicorn

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig, default_data_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the local FBE Flow application")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(create_app(AppConfig(args.data_dir.resolve())), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
