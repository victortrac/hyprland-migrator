#!/usr/bin/env python3
"""Build a portable executable archive using only the Python standard library."""

import argparse
from pathlib import Path
import shutil
import tempfile
import zipapp


ROOT = Path(__file__).resolve().parent.parent
PACKAGE_FILES = ("__init__.py", "__main__.py", "cli.py", "converter.py", "options.json")


def build(output):
    output = Path(output).expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hyprland-migrator-build-") as directory:
        staging = Path(directory)
        package = staging / "hyprland_migrator"
        package.mkdir()
        # Explicit allowlist excludes personal configs, caches, tests and metadata.
        for name in PACKAGE_FILES:
            shutil.copyfile(ROOT / "hyprland_migrator" / name, package / name)
        (staging / "__main__.py").write_text(
            "from hyprland_migrator.cli import main\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        zipapp.create_archive(staging, target=output, interpreter="/usr/bin/env python3")
    output.chmod(0o755)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=ROOT / "dist/hyprland-migrator.pyz")
    args = parser.parse_args()
    output = build(args.output)
    print(f"Built {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
