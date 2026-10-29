"""Command line interface. Conversion never executes configuration commands."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .converter import Converter, Unsupported


def safe_write(path, contents, force=False):
    """Publish a complete file; preserve existing content before explicit replacement."""
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise OSError(f"refusing to replace symlink {path}")
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; use --force to replace it with a backup")
    if not path.parent.is_dir():
        raise OSError(f"output directory does not exist: {path.parent}")
    backup = None
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if force and path.exists():
            # Exclusive backup creation, including when two runs race.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = path.with_name(path.name + ".bak." + stamp)
            with backup.open("xb") as target:
                os.chmod(backup, 0o600)
                target.write(path.read_bytes())
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        else:
            # link fails atomically if a destination appeared after preflight.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def parser():
    default = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "hypr/hyprland.conf"
    result = argparse.ArgumentParser(description="Convert legacy Hyprland .conf to native Lua for 0.57. Originals are never modified.")
    result.add_argument("input", nargs="?", type=Path, default=default, help=f"entry config (default: {default})")
    result.add_argument("-o", "--output", type=Path, help="write a new Lua file (default: stdout)")
    result.add_argument("--dry-run", action="store_true", help="print Lua to stdout without writing output or report")
    result.add_argument("--check", action="store_true", help="only report conversion diagnostics; write nothing")
    result.add_argument("--allow-partial", action="store_true", help="emit incomplete Lua with TODO comments; still exit 1")
    result.add_argument("--force", action="store_true", help="back up and replace existing output/report files")
    result.add_argument("--report", type=Path, help="write JSON diagnostics and input file list")
    result.add_argument("--version", action="version", version=__version__)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = Converter().convert(args.input)
        destinations = [p.expanduser().absolute() for p in (args.output, args.report) if p]
        if len({p.resolve() for p in destinations}) != len(destinations):
            raise OSError("output and report must use different paths")
        for path in destinations:
            if path.resolve() in result.inputs or any(path.exists() and os.path.samefile(path, source) for source in result.inputs):
                raise OSError(f"refusing to overwrite input config {path}")
            if not (args.dry_run or args.check):
                if path.is_symlink():
                    raise OSError(f"refusing to replace symlink {path}")
                if path.exists() and not args.force:
                    raise OSError(f"{path} exists; use --force to replace it with a backup")
                if not path.parent.is_dir():
                    raise OSError(f"output directory does not exist: {path.parent}")
        for diagnostic in result.diagnostics:
            print(f"{diagnostic.path}:{diagnostic.line}: {diagnostic.severity}: {diagnostic.message}", file=sys.stderr)
        if args.report and not (args.dry_run or args.check):
            backup = safe_write(args.report, json.dumps(result.report(), indent=2) + "\n", args.force)
            if backup:
                print(f"Report backup: {backup}", file=sys.stderr)
        if result.errors and not args.allow_partial:
            print(f"Conversion incomplete: {len(result.errors)} error(s). No Lua emitted. See diagnostics or use --allow-partial for a review draft.", file=sys.stderr)
            return 1
        if not args.check:
            if args.output and not args.dry_run:
                backup = safe_write(args.output, result.lua, args.force)
                print(f"Wrote {args.output}", file=sys.stderr)
                if backup:
                    print(f"Backup: {backup}", file=sys.stderr)
            else:
                sys.stdout.write(result.lua)
        print(f"Converted {result.converted} directives from {len(result.inputs)} file(s); {len(result.errors)} error(s).", file=sys.stderr)
        return 1 if result.errors else 0
    except BrokenPipeError:
        return 2
    except (OSError, Unsupported) as error:
        print(f"hyprland-migrator: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
