# Builds dist\BIMForge\BIMForge.exe (one-folder build, so LGPL libraries stay replaceable).
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

& $python -m pip install --quiet "pyinstaller>=6.5"
& $python -m pip install --quiet -e "$root[all]"

& $python -m PyInstaller `
    --noconfirm --clean --windowed `
    --name BIMForge `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    --collect-all ifcopenshell `
    --collect-all ezdxf `
    --collect-all pdfminer `
    --collect-all pypdfium2 `
    --collect-all pypdfium2_raw `
    --collect-all fcl `
    --collect-submodules vtkmodules `
    --copy-metadata bimforge `
    (Join-Path $PSScriptRoot "launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# License notices for bundled LGPL components (Qt/PySide6, IfcOpenShell, GEOS).
Copy-Item (Join-Path $PSScriptRoot "THIRD_PARTY_NOTICES.txt") (Join-Path $root "dist\BIMForge") -Force
Write-Host "Built $(Join-Path $root 'dist\BIMForge\BIMForge.exe')"
