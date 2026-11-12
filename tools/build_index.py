#!/usr/bin/env python3
"""Regenerate index.json, the catalog sensor-patchbay browses and installs from.

    python tools/build_index.py           # rewrite index.json (modules and presets)
    python tools/build_index.py --check   # exit 1 if index.json is stale (CI)

Every folder under sources/ and sinks/ with a module.toml is a module. Its
`entry` (default __init__.py) must exist. Its entry lists every file (relative to this repo's root, which is
also where index.json sits, so a client resolves them against the index URL)
with its sha256, plus the manifest fields the catalog shows.

Files skipped: dotfiles, __pycache__, *.pyc, and the module's declared
[[assets]] (those are downloaded by the engine's sync, not shipped). The
engine skips the same ones when it checks whether an installed module is up
to date, so keep the two in step (engine/catalog.py, _local_digest).

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.json"
KINDS = {"sources": "source", "sinks": "sink"}
SKIP_PARTS = {"__pycache__", ".DS_Store"}
VERSION = re.compile(r"\d+(\.\d+)*")


def module_digest(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]}\n".encode())
    return h.hexdigest()


def check_entry(manifest: Path, value) -> str:
    p = PurePosixPath(str(value).replace("\\", "/"))
    if not str(value).strip() or p.is_absolute() or ".." in p.parts:
        sys.exit(f"{manifest}: entry {value!r} must be a path inside the module folder")
    return str(p)


def entry(folder: Path, kind: str) -> dict | None:
    manifest = folder / "module.toml"
    if not manifest.is_file():
        return None
    data = tomllib.loads(manifest.read_text())
    main = check_entry(manifest, data.get("entry", "__init__.py"))
    if not (folder / main).is_file():
        sys.exit(f"{manifest}: entry {main} doesn't exist")
    version = str(data.get("version", ""))
    if not VERSION.fullmatch(version):
        sys.exit(f"{manifest}: needs version = \"X.Y.Z\" (dotted numbers), got {version!r}")
    if data.get("kind", kind) != kind:
        sys.exit(f"{manifest}: kind {data['kind']!r} doesn't match its folder ({kind})")
    if not str(data.get("description", "")).strip():
        sys.exit(f"{manifest}: needs a one-line description")
    assets = {a.get("file") for a in data.get("assets", [])}
    files, listed = {}, []
    for p in sorted(folder.rglob("*")):
        inner = p.relative_to(folder)
        rel = inner.as_posix()
        if (not p.is_file() or rel in assets or p.suffix in (".pyc", ".part")
                or any(part in SKIP_PARTS or part.startswith(".") for part in inner.parts)):
            continue
        blob = p.read_bytes()
        files[rel] = hashlib.sha256(blob).hexdigest()
        listed.append({"path": p.relative_to(ROOT).as_posix(), "sha256": files[rel],
                       "size": len(blob)})
    return {
        "name": folder.name,
        "kind": kind,
        "description": data.get("description", ""),
        "version": version,
        "entry": main,
        "depends": data.get("depends", []),
        "python": data.get("python", []),
        "system": data.get("system", []),
        "digest": module_digest(files),
        "files": listed,
    }


def build() -> str:
    mods = []
    for parent, kind in KINDS.items():
        for folder in sorted((ROOT / parent).iterdir()):
            if folder.is_dir() and not folder.name.startswith(("_", ".")):
                e = entry(folder, kind)
                if e:
                    mods.append(e)
    names = [m["name"] for m in mods]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"module names must be unique across sources/ and sinks/: {dupes}")
    for m in mods:
        missing = [d for d in m["depends"] if d not in names]
        if missing:
            sys.exit(f"{m['name']} depends on {missing}, which aren't in this repo")
    return json.dumps({"format": 1, "modules": mods, "presets": presets(mods)}, indent=1) + "\n"


PRESET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")


def presets(mods: list[dict]) -> list[dict]:
    """Every presets/*.json, checked: a name matching the file, a connections
    list, and a `requires` entry (naming modules in this repo) for every
    namespace the connections use."""
    kinds = {m["name"]: m["kind"] for m in mods}
    out = []
    for f in sorted((ROOT / "presets").glob("*.json")):
        name = f.stem
        if not PRESET_NAME.fullmatch(name) or name.endswith("."):
            sys.exit(f"{f}: preset names are letters, digits, space, . _ -")
        try:
            d = json.loads(f.read_text())
        except ValueError as e:
            sys.exit(f"{f}: not valid JSON ({e})")
        if not isinstance(d, dict) or not isinstance(d.get("connections"), list):
            sys.exit(f"{f}: needs a \"connections\" list")
        if d.get("name", name) != name:
            sys.exit(f"{f}: \"name\" is {d['name']!r}; it must match the file name")
        if not str(d.get("description", "")).strip():
            sys.exit(f"{f}: needs a one-line \"description\"")
        reqs = d.get("requires")
        if not isinstance(reqs, list):
            sys.exit(f"{f}: needs a \"requires\" list")
        covered = set()
        for r in reqs:
            types = r.get("types") or []
            if r.get("kind") not in ("source", "sink") or not r.get("id") or not types:
                sys.exit(f"{f}: each requires entry needs id, kind and types: {r}")
            for t in types:
                if kinds.get(t) != r["kind"]:
                    sys.exit(f"{f}: requires {r['kind']} type {t!r}, which isn't a {r['kind']} in this repo")
            covered.add((r["kind"], r["id"]))
        for c in d["connections"]:
            for kind, port in (("source", c.get("source", "")), ("sink", c.get("sink", ""))):
                ns = str(port).partition("/")[0]
                if ns and ns != "sys" and (kind, ns) not in covered:
                    sys.exit(f"{f}: connection {c.get('id')} uses {kind} {ns!r}, missing from requires")
        blob = f.read_bytes()
        out.append({"name": name, "description": d["description"], "requires": reqs,
                    "path": f.relative_to(ROOT).as_posix(),
                    "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)})
    return out


def main() -> int:
    text = build()
    if "--check" in sys.argv[1:]:
        if not INDEX.is_file() or INDEX.read_text() != text:
            print("index.json is out of date: run python tools/build_index.py", file=sys.stderr)
            return 1
        print("index.json is up to date")
        return 0
    INDEX.write_text(text)
    print(f"wrote {INDEX.relative_to(ROOT)} ({len(json.loads(text)['modules'])} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
