"""Run BIMForge from a source checkout without installing it: python index.py [view|convert|sample] ..."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bimforge.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
