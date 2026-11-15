"""Download raw NASA FIRMS data (or labelled synthetic demo data when no MAP_KEY is set).

Run from the repository root:

    python src/fetch_data.py --plan-only      # show what would be requested; makes no API calls
    python src/fetch_data.py                  # fetch the recent window (raw files only)
    python src/fetch_data.py --start 2025-01-01 --end 2025-03-31

This is a thin wrapper around `python -m emberwatch fetch`. The MAP_KEY is read from the
FIRMS_MAP_KEY environment variable only (see .env.example).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from emberwatch.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["fetch"] + sys.argv[1:]))
