from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()

    if not args.executable.is_file():
        raise FileNotFoundError(args.executable)

    result = subprocess.run(
        [str(args.executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
