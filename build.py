#!/usr/bin/env python3
"""Build the single-file Open WebUI plugin (v1.6.1.py) from the src/ fragments.

Usage:
    python build.py

The fragments in src/ are NOT importable on their own — they share the
imports/constants defined in _header.py. This script concatenates them, in
dependency order, into one self-contained v1.6.1.py ready to paste into
Open WebUI. Edit the fragments, then run this to rebuild.
"""
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "src"
OUT = HERE / "v1.6.1.py"

# Order matters: shared header first; Filter (inherits the mixins) last.
MODULES = [
    "_header",
    "i18n",
    "tokens",
    "db",
    "toolcalls",
    "compression",
    "summarize",
    "externalrefs",
    "console",
    "filter",
]


def main():
    parts = []
    for name in MODULES:
        p = SRC / f"{name}.py"
        if not p.exists():
            raise SystemExit(f"missing fragment: {p}")
        parts.append(p.read_text(encoding="utf-8").rstrip("\n"))
    out = "\n\n".join(parts) + "\n"
    OUT.write_text(out, encoding="utf-8")
    print(f"built {OUT.name}: {len(out.splitlines())} lines from {len(MODULES)} fragments")


if __name__ == "__main__":
    main()
