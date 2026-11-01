"""Entry point frozen by PyInstaller into BIMForge.exe."""

import sys

from bimforge.__main__ import main

if __name__ == "__main__":
    # Double-clicking the exe (no arguments) opens the app; arguments work as on the command line.
    sys.exit(main(sys.argv[1:] or ["view"]))
