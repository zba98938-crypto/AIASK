from __future__ import annotations

import argparse
import json
from pathlib import Path

from strategy_factory.application.futures_calendar_research import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SC_DATA_PATH,
    run_sc_calendar_research,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SC futures calendar research adapter.")
    parser.add_argument("--data-path", type=Path, default=DEFAULT_SC_DATA_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--online-generalization", action="store_true")
    args = parser.parse_args()

    result = run_sc_calendar_research(
        data_path=args.data_path,
        output_dir=args.output_dir,
        enable_online_generalization=bool(args.online_generalization),
    )
    print(json.dumps(result.get("output_paths") or {}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
