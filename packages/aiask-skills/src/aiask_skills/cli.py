from __future__ import annotations

import argparse
import json

from . import PROTOCOL_VERSION, VERSION, bundle_path, discover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiask-skills")
    parser.add_argument("--json", action="store_true")
    parser.parse_args(argv)
    print(json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "version": VERSION,
        "bundle_path": str(bundle_path()),
        "skills": discover(),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

