"""Validate a recording and import it as a read-only snapshot, never live accounts."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db", type=Path, help="Import into an explicitly chosen local database")
    args = parser.parse_args()
    from app.config import load_settings
    from app.replay import import_recording
    from app.store import Store

    try:
        settings = load_settings(args.env_file)
        with args.recording.open(encoding="utf-8") as source:
            recording = json.load(source)
        result = import_recording(Store(args.db or settings.db_path), recording)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Import failed: {type(error).__name__}. Check file format and permissions.\n")
    print(f"Imported read-only recording: {result['id']}")


if __name__ == "__main__":
    main()
