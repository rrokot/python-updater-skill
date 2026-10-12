#!/usr/bin/env python3
"""Which Python a project accepts: its requires-python, and the newest release that meets it.

Shared by both phases. update_python.ps1 runs it as a script to pick the version the venv
is rebuilt on — PowerShell cannot evaluate PEP 440 — and update_python.py imports it to
refuse a target the project does not accept.

Stdlib only: it runs on whatever interpreter is at hand, a fresh install included.
Versions are plain releases (X.Y.Z) — the stable catalog of pymanager has nothing else —
so the pre/post/dev parts of PEP 440 are not handled.

Usage:
    python project_python.py -p PROJECT VERSION [VERSION ...]

Prints the newest VERSION the project accepts (the newest overall when it states no
constraint). Exit 2: none of them is accepted. Exit 1: pyproject.toml cannot be read.
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path


class PyprojectError(Exception):
    """pyproject.toml exists but is not valid TOML."""


class ConstraintError(Exception):
    """The constraint is in a syntax this module does not understand."""


def release(ver: str) -> tuple[int, ...]:
    return tuple(int(x) for x in ver.split("."))


def python_constraint(project: Path) -> str | None:
    path = project / "pyproject.toml"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        # Not "no constraint": uv and poetry cannot work with this file either, and a
        # rebuild started on a guess would only fail later, after the venv is torn down.
        raise PyprojectError(f"{path} is not valid TOML ({e})")
    spec = data.get("project", {}).get("requires-python")
    if spec is None:
        # Poetry 1.x projects keep it among the dependencies, in Poetry's own syntax.
        spec = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
    if spec is not None and not isinstance(spec, str):
        raise ConstraintError(f"python constraint {spec!r} is not a string")
    return spec


_CLAUSE = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>|\^|~)?\s*(\*|\d+(?:\.\d+)*(?:\.\*)?)\s*")


def _clauses(group: str) -> list[tuple[str, str]]:
    # PEP 440 separates clauses with commas; Poetry also accepts spaces (">=3.8 <4").
    out = []
    for piece in group.split(","):
        if not piece.strip():
            raise ConstraintError(f"cannot parse {group!r}")
        pos = 0
        while pos < len(piece):
            m = _CLAUSE.match(piece, pos)
            if not m or m.end() == pos:
                raise ConstraintError(f"cannot parse {group!r}")
            out.append((m.group(1) or "", m.group(2)))
            pos = m.end()
    return out


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _prefix(ver: tuple[int, ...], base: tuple[int, ...]) -> bool:
    return (ver + (0,) * len(base))[:len(base)] == base


def _matches(ver_str: str, op: str, spec: str) -> bool:
    if spec == "*":
        return op != "!="
    ver = release(ver_str)
    wildcard = spec.endswith(".*")
    base = release(spec[:-2] if wildcard else spec)

    if wildcard or (op == "" and len(base) < 3):
        # A bare X.Y, the way Poetry writes its python dependency, means that minor.
        if op in ("", "=="):
            return _prefix(ver, base)
        if op == "!=":
            return not _prefix(ver, base)
        raise ConstraintError(f"{op}{spec} is not a valid clause")

    if op in ("", "=="):
        return _cmp(ver, base) == 0
    if op == "===":
        return ver_str == spec
    if op == "!=":
        return _cmp(ver, base) != 0
    if op == ">=":
        return _cmp(ver, base) >= 0
    if op == ">":
        return _cmp(ver, base) > 0
    if op == "<=":
        return _cmp(ver, base) <= 0
    if op == "<":
        return _cmp(ver, base) < 0
    if op == "~=":
        if len(base) < 2:
            raise ConstraintError(f"~={spec} needs at least two components")
        return _cmp(ver, base) >= 0 and _prefix(ver, base[:-1])

    # Poetry: ^ keeps the leftmost non-zero component, ~ keeps the minor (the major
    # when only a major is given).
    if op == "^":
        i = next((k for k, v in enumerate(base) if v), len(base) - 1)
    else:
        i = 1 if len(base) >= 2 else 0
    upper = base[:i] + (base[i] + 1,)
    return _cmp(ver, base) >= 0 and _cmp(ver, upper) < 0


def satisfies(version: str, constraint: str) -> bool:
    # "||" is Poetry's union; PEP 440 has none.
    return any(
        all(_matches(version, op, spec) for op, spec in _clauses(group))
        for group in constraint.split("||")
    )


def newest_accepted(versions: list[str], constraint: str | None) -> str | None:
    ok = [v for v in versions if constraint is None or satisfies(v, constraint)]
    return max(ok, key=release) if ok else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pick the newest Python release the project accepts.")
    parser.add_argument("-p", "--project", default=".", help="project directory (default: cwd)")
    parser.add_argument("versions", nargs="+", help="candidate releases, X.Y.Z")
    args = parser.parse_args(argv)

    try:
        constraint = python_constraint(Path(args.project))
        picked = newest_accepted(args.versions, constraint)
    except PyprojectError as e:
        print(f"[update-python] WARNING: {e} — fix it, then re-run", file=sys.stderr)
        return 1
    except ConstraintError as e:
        # Unreadable is not a reason to stop: fall back to what the updater did before it
        # knew about requires-python, and let the venv manager have the final word.
        print(f"[update-python] WARNING: requires-python not understood ({e}) — not applied", file=sys.stderr)
        constraint, picked = None, newest_accepted(args.versions, None)

    if picked is None:
        print(f"[update-python] WARNING: no stable Python satisfies requires-python {constraint!r}",
              file=sys.stderr)
        return 2
    if constraint is not None:
        print(f"[update-python] requires-python {constraint!r}: newest accepted is {picked}", file=sys.stderr)
    print(picked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
