"""DWG importer: converts DWG to DXF with an external, user-installed converter, then reads the DXF.

DWG is a proprietary Autodesk format. BIMForge does not bundle a DWG reader:
* ODA File Converter (free download from the Open Design Alliance; its licence
  does not allow redistribution) is used when installed, or
* LibreDWG's dwg2dxf (GPL-3) when found on PATH; it runs as a separate program,
  so no GPL code is linked into BIMForge.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bimforge.adapters.dxf_importer import DxfImporter
from bimforge.plugins.sdk import Importer, ImportResult, PluginInfo

ODA_DOWNLOAD = "https://www.opendesign.com/guestfiles/oda_file_converter"
CONVERT_TIMEOUT_S = 300


def find_oda_converter() -> str | None:
    env = os.environ.get("BIMFORGE_ODA_CONVERTER")
    if env and Path(env).is_file():
        return env
    found = shutil.which("ODAFileConverter")
    if found:
        return found
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
    candidates = [c for root in roots if root
                  for c in glob.glob(os.path.join(root, "ODA", "ODAFileConverter*", "ODAFileConverter.exe"))]
    for linux in ("/usr/bin/ODAFileConverter", "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"):
        if Path(linux).is_file():
            candidates.append(linux)
    # Newest version folder last; compare version numbers numerically ("25.12" is newer than "25.9").
    return max(candidates, key=_version_key) if candidates else None


def _version_key(path: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", Path(path).parent.name)), path


def find_libredwg() -> str | None:
    return shutil.which("dwg2dxf")


class DwgImporter(Importer):
    info = PluginInfo(id="bimforge.import.dwg", name="DWG (via ODA File Converter or LibreDWG)", version="0.1.0")
    extensions = (".dwg",)

    def import_file(self, path: Path, options: Mapping[str, Any] | None = None) -> ImportResult:
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"File not found: {path}")
        with tempfile.TemporaryDirectory(prefix="bimforge-dwg-") as tmp:
            dxf, tool = self._convert(path, Path(tmp))
            result = DxfImporter().import_file(dxf, options)
        assert result.drawing is not None
        result.drawing.source = str(path)
        note = f"DWG converted to DXF with {tool}"
        result.drawing.warnings.insert(0, note)
        result.warnings.insert(0, note)
        return result

    def _convert(self, source: Path, out_dir: Path) -> tuple[Path, str]:
        oda = find_oda_converter()
        if oda:
            in_dir = out_dir / "in"
            in_dir.mkdir()
            shutil.copy2(source, in_dir / source.name)  # ODA converts whole folders
            # Arguments: input folder, output folder, output version, output type, recurse, audit.
            self._run([oda, str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1"])
            dxf = out_dir / (source.stem + ".dxf")
            if dxf.is_file():
                return dxf, "ODA File Converter"
            raise ValueError("ODA File Converter did not produce a DXF; the DWG may be damaged or password protected")
        libredwg = find_libredwg()
        if libredwg:
            dxf = out_dir / (source.stem + ".dxf")
            self._run([libredwg, "-y", "-o", str(dxf), str(source)])
            if dxf.is_file():
                return dxf, "LibreDWG dwg2dxf"
            raise ValueError("dwg2dxf did not produce a DXF; the DWG may use features LibreDWG cannot read")
        raise ValueError(
            "Opening DWG files needs the free ODA File Converter, which is not installed.\n\n"
            f"1. Download and install it from {ODA_DOWNLOAD}\n"
            "2. Import the DWG again.\n\n"
            "Alternatively, save the drawing as DXF from your CAD program (File > Save As > DXF) and import that."
        )

    @staticmethod
    def _run(command: list[str]) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(command, capture_output=True, timeout=CONVERT_TIMEOUT_S, creationflags=flags)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("DWG conversion timed out") from exc
        except OSError as exc:
            raise ValueError(f"Could not start the DWG converter: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).decode(errors="replace").strip()[:500]
            raise ValueError(f"DWG conversion failed (exit {completed.returncode}): {detail}")
