"""Clean raw FIRMS files into the common observation schema, then rebuild derived data and exports.

Run from the repository root:

    python src/harmonize.py              # process any new raw files
    python src/harmonize.py --rebuild    # reprocess every raw file from scratch

This is a thin wrapper around `python -m emberwatch harmonize`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from emberwatch.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["harmonize"] + sys.argv[1:]))
