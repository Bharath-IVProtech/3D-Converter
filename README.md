# BIMForge

AI-assisted conversion of 2D drawings into parametric 3D BIM models, with plumbing, electrical and coordination tools.

What works today:
- **2D → 3D:** DXF plan → detected walls, doors, windows, rooms and slab → review with confidence scores → 3D model.
- **Architecture:** multi-level projects, editing with validation, connected walls follow moves, copy/duplicate levels.
- **IFC:** IFC4 import (walls, slabs, doors, windows and rooms become editable; everything else kept as reference geometry) and export, including MEP and systems.
- **Plumbing:** fixtures, drainage/supply systems, drawn pipes with automatic drainage grading, network sizing (IPC tables).
- **Electrical:** sockets, lights, switches, boards, circuits, conduit and cable tray, panel schedules, single-line diagram (SVG).
- **Coordination:** clash detection (hard and clearance) with issue tracking, model checks, quantity takeoff to Excel/CSV, section box, measure.

**New here?** Read [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md), a plain-language, step-by-step guide to how BIMForge is designed and how it turns a drawing into a 3D model.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture, data model, pipelines, roadmap and risks.

## Setup (Windows)

Python 3.11–3.13 is required. IfcOpenShell, VTK and PySide6 may not have wheels for newer Python versions yet.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[all]"
```

## Run

```powershell
.\.venv\Scripts\python index.py sample samples\two_rooms.dxf            # write a demo plan
.\.venv\Scripts\python index.py view samples\two_rooms.dxf              # desktop app
.\.venv\Scripts\python index.py convert samples\two_rooms.dxf out.ifc   # headless (.ifc or .bimx)
.\.venv\Scripts\python index.py view house.bimx                          # open a saved project
.\.venv\Scripts\python -m pytest                                         # tests
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1    # build dist\BIMForge\BIMForge.exe
```

### In the app

1. **File > Import…** (Ctrl+I) opens a DXF plan, or an IFC model.
2. Detected elements appear as orange previews over the 2D underlay.
3. Accept or reject them in **AI review**, then **Commit to model**.
4. **Ctrl+Z** undoes the whole conversion.
5. **Export** writes IFC.
6. **Conversion settings** changes the defaults (wall height, sill height and so on) and re-runs detection.

**Editing.** Click an element in the 3D view or the **Project browser**. Ctrl+click or Ctrl+A selects several.
- **Change a property:** double-click a value in **Properties**. Lengths are in metres, and points are typed as `x, y`.
- **Move:** Ctrl+M opens the Move dialog. Doors and windows slide along their wall and stay inside it.
- **Delete:** the Del key. Deleting a wall also deletes its openings.
- **Validation:** an edit is rejected with an explanation if it would break the model (e.g. a window taller than its wall, or overlapping openings).
- **Undo/redo** covers every edit.

**Levels.** Use the **Levels** panel to add, duplicate (copies a typical floor) or delete levels, and to edit names and elevations.
- The **Level** box in the toolbar sets the active level. Imports and drawing go there.
- Ctrl+L shows only the active level.

**MEP** (MEP panel, right):
1. **New…** creates a system (e.g. sanitary, cold water) or a circuit (power/lighting, fed from a distribution board).
2. Select the system, then **Place fixture** or **Draw run** and click in the view.
   - Points snap to the grid and to fixture connection points. Hold Shift for orthogonal segments.
   - Press Enter or right-click to finish a run, Esc to stop.
   - Drainage pipes are automatically graded to the code minimum slope.
3. **Size pipes** sizes a whole system from fixture units.
4. **Panel schedule…** shows circuit loads, breakers, cables and voltage drop, and exports CSV and a single-line diagram.

**Coordination** (Coordination panel):
- **Run clash detection** and **Run model checks**. Double-click an issue to zoom to it, and set its status (new, active, resolved, approved).
- Statuses and comments are kept when tests are re-run.
- **File > Export quantities (BOQ)** writes Excel or CSV.
- **View > Section box** (Ctrl+B) clips the view. **Tools > Measure** (M) measures distances.

**Projects.** Projects save as single-file `.bimx` (SQLite) with Ctrl+S, Ctrl+Shift+S and Ctrl+O.
- **Autosave** runs every 2 minutes, to `<project>.bimx.autosave`, or to the app data folder for untitled projects.
- **Recovery:** after a crash, you're offered the autosaved version the next time you open the project or start the app.
- An asterisk in the window title means there are unsaved changes.

### 2D drawing input (DXF / DWG / PDF)

- **DXF:** opened directly.
  - Plans drawn inside blocks are exploded automatically.
  - Wide polylines and multilines (MLINE) are read as walls.
  - Plans drawn only in paper space are read too.
- **DWG:** install the free **ODA File Converter** (https://www.opendesign.com/guestfiles/oda_file_converter), then import the `.dwg` as usual.
  - BIMForge finds it automatically. To point at a specific copy, set `BIMFORGE_ODA_CONVERTER` to the full path of `ODAFileConverter.exe`.
  - LibreDWG's `dwg2dxf` on PATH also works.
  - Without either, use **Save As → DXF** in your CAD program.
- **PDF (vector):** plans exported or plotted to PDF from CAD.
  - **Scale:** read from text like "SCALE 1:100" on the sheet. You confirm or change it when importing; on the command line use `--scale 100 --page 2`.
  - **Line weights:** PDFs have no layers, so lines are grouped by weight. Tick the wall line weights in Conversion settings if walls are missed.
  - **Doors and windows:** doors are found from their swing arcs, windows from glazing lines across wall gaps.
  - **Scanned PDFs and images** (no vector lines) aren't supported yet.
- **What is detected** (DXF, DWG and PDF):
  - **Walls:** pairs of parallel face lines (the two faces may use different line weights), wide polylines, hatched or solid-filled (poché) bands, and a single face line with hatching along one side.
  - **Doors:** blocks, or swing arcs (true arcs or arcs flattened into short lines, including double doors).
  - **Windows:** blocks, or glazing lines across a wall gap.
  - **Stairs:** runs of 4 or more evenly spaced treads at any angle. Check the climbing direction and height before committing.
  - **Columns:** small filled rectangles.
  - **Rooms and floor slab:** derived from the walls.
- **Short isolated wall pieces** (often equipment plinths) are proposed but left unticked.
- **Check a PDF's conversion** by overlaying the detected elements on the sheet: `python -m bimforge.devtools.overlay plan.pdf check.png`.
- **Walls** are detected as pairs of parallel lines 50–600 mm apart, or as wide polylines.
  - If none are found, the import notes list your busiest layers. Tick the wall layers in **Edit > Conversion settings** and the drawing is re-converted.
- **Doors and windows:** block references whose block or layer name contains door/window keywords (`DOOR`, `DR`, `WIN`, `GLAZ`, `FENSTER`…).
- **Units:** read from the file. If the file has none, they're guessed and a note is shown. Override them in Conversion settings or with `--units`.
- **External references (xrefs)** aren't loaded. Bind them in your CAD program first.
