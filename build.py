"""Pack src/addon into dist/remoteSpeechControl-<version>.nvda-addon.

A .nvda-addon file is a plain ZIP with manifest.ini at the archive root.
Run with: py -3.11 build.py
"""
from __future__ import annotations

import configparser
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src" / "addon"
DIST = ROOT / "dist"
MANIFEST = SRC / "manifest.ini"


def parse_manifest() -> tuple[str, str]:
    parser = configparser.ConfigParser()
    parser.read_string("[default]\n" + MANIFEST.read_text(encoding="utf-8"))
    def unquote(v: str) -> str:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        return v
    name = unquote(parser["default"]["name"])
    version = unquote(parser["default"]["version"])
    return name, version


def main() -> int:
    if not SRC.is_dir():
        print(f"Source folder missing: {SRC}", file=sys.stderr)
        return 1
    DIST.mkdir(exist_ok=True)
    name, version = parse_manifest()
    for stale in DIST.glob(f"{name}-*.nvda-addon"):
        stale.unlink()
    out = DIST / f"{name}-{version}.nvda-addon"

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SRC.rglob("*")):
            if path.is_dir():
                continue
            if "__pycache__" in path.parts:
                continue
            arcname = path.relative_to(SRC).as_posix()
            zf.write(path, arcname)

    print(f"Built: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
