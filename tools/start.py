"""Start one loopback-only service without exposing the private UI."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("ui", "receiver"))
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.env_file and not args.env_file.is_file():
        parser.error("--env-file must point to an existing file")

    import uvicorn
    from app.config import load_settings

    settings = load_settings(args.env_file)
    if args.service == "ui":
        from app.main import create_app
        port = 8787
    else:
        from app.receiver import create_app
        port = 8788
    # Access logs could reveal opaque webhook path tokens.
    uvicorn.run(create_app(settings=settings), host="127.0.0.1", port=port,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
