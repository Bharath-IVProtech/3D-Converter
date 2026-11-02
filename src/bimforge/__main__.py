"""Command line entry point.

    bimforge view [drawing.dxf | project.bimx]  open the desktop app
    bimforge convert plan.dxf|.dwg|.pdf model.ifc|.bimx   headless conversion (accepts proposals >= threshold)
    bimforge sample samples/two_rooms.dxf       write a demo plan
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bimforge import __version__


def _convert(args: argparse.Namespace) -> int:
    from bimforge.app.commands import CommandBus, commit_conversion
    from bimforge.app.events import EventBus
    from bimforge.app.persistence import FILE_EXTENSION, save_project
    from bimforge.conversion import ConversionSettings, PlanConverter
    from bimforge.core.model import Project
    from bimforge.core.units import LengthUnit
    from bimforge.plugins.registry import PluginRegistry

    registry = PluginRegistry.with_builtins()
    options = {k: v for k, v in (("scale", args.scale), ("page", args.page)) if v}
    imported = registry.importer_for(args.input).import_file(args.input, options)
    if imported.drawing is None:
        print("Input contains no 2D drawing", file=sys.stderr)
        return 2

    settings = ConversionSettings(wall_height=args.wall_height, accept_threshold=args.min_confidence)
    if args.units:
        settings.unit_override = LengthUnit.from_symbol(args.units)
    project = Project(args.input.stem)
    result = PlanConverter(settings).run(imported.drawing, project.default_storey)
    for warning in imported.warnings + result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for p in result.proposals:
        mark = "+" if p.accepted else "-"
        print(f"  {mark} {p.kind:<7} {p.element.name:<12} {p.confidence:5.0%}  {p.note}")

    CommandBus(project, EventBus()).execute(commit_conversion(result, project))
    if args.output.suffix.lower() == FILE_EXTENSION:
        save_project(project, args.output)
        warnings = []
    else:
        warnings = registry.exporter_for(args.output).export(project, args.output)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Wrote {args.output} with {len(project)} elements")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bimforge", description="2D drawings to parametric 3D BIM")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    view = sub.add_parser("view", help="open the desktop application")
    view.add_argument("input", type=Path, nargs="?")

    convert = sub.add_parser("convert", help="convert a drawing without the UI")
    convert.add_argument("input", type=Path)
    convert.add_argument("output", type=Path)
    convert.add_argument("--wall-height", type=float, default=3.0)
    convert.add_argument("--min-confidence", type=float, default=0.5)
    convert.add_argument("--units", choices=["mm", "cm", "m", "in", "ft"], help="override drawing units")
    convert.add_argument("--scale", type=int, help="PDF drawing scale, e.g. 100 for 1:100 (default: read from sheet)")
    convert.add_argument("--page", type=int, help="PDF page number (default 1)")

    sample = sub.add_parser("sample", help="write a sample floor plan DXF")
    sample.add_argument("output", type=Path, nargs="?", default=Path("samples/two_rooms.dxf"))

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    if args.command == "convert":
        return _convert(args)
    if args.command == "sample":
        from bimforge.devtools.sample_plan import build_sample_plan

        print(f"Wrote {build_sample_plan(args.output)}")
        return 0

    from bimforge.ui.main_window import run

    return run(getattr(args, "input", None))


if __name__ == "__main__":
    sys.exit(main())
