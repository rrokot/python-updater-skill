#!/usr/bin/env python3
import argparse
import re
import sys
import tomllib
from pathlib import Path


class PyprojectError(Exception):
    pass


class ConstraintError(Exception):
    pass


def release_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def python_constraint(project: Path) -> str | None:
    pyproject_path = project / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        with pyproject_path.open("rb") as pyproject_file:
            pyproject = tomllib.load(pyproject_file)
    except tomllib.TOMLDecodeError as error:
        raise PyprojectError(f"{pyproject_path} is not valid TOML ({error})")
    constraint = pyproject.get("project", {}).get("requires-python")
    if constraint is None:
        poetry_dependencies = pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {})
        constraint = poetry_dependencies.get("python")
    if constraint is not None and not isinstance(constraint, str):
        raise ConstraintError(f"python constraint {constraint!r} is not a string")
    return constraint


_VERSION_CLAUSE_PATTERN = re.compile(
    r"\s*(?P<operator>~=|===|==|!=|<=|>=|<|>|\^|~)?"
    r"\s*(?P<version>\*|\d+(?:\.\d+)*(?:\.\*)?)\s*"
)


def _parse_version_clauses(constraint_group: str) -> list[tuple[str, str]]:
    clauses = []
    for comma_separated_group in constraint_group.split(","):
        if not comma_separated_group.strip():
            raise ConstraintError(f"cannot parse {constraint_group!r}")
        clause_start = 0
        while clause_start < len(comma_separated_group):
            clause = _VERSION_CLAUSE_PATTERN.match(comma_separated_group, clause_start)
            if clause is None:
                raise ConstraintError(f"cannot parse {constraint_group!r}")
            clauses.append((clause.group("operator") or "", clause.group("version")))
            clause_start = clause.end()
    return clauses


def _compare_release_parts(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    length = max(len(left), len(right))
    padded_left = left + (0,) * (length - len(left))
    padded_right = right + (0,) * (length - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _has_release_prefix(version: tuple[int, ...], prefix: tuple[int, ...]) -> bool:
    return (version + (0,) * len(prefix))[:len(prefix)] == prefix


def _matches_version_clause(version: str, operator: str, specified_version: str) -> bool:
    if specified_version == "*":
        return operator != "!="
    candidate = release_parts(version)
    has_wildcard = specified_version.endswith(".*")
    specified_parts = release_parts(specified_version[:-2] if has_wildcard else specified_version)

    is_bare_release_prefix = operator == "" and len(specified_parts) < 3
    if has_wildcard or is_bare_release_prefix:
        if operator in ("", "=="):
            return _has_release_prefix(candidate, specified_parts)
        if operator == "!=":
            return not _has_release_prefix(candidate, specified_parts)
        raise ConstraintError(f"{operator}{specified_version} is not a valid clause")

    if operator == "===":
        return version == specified_version

    comparison = _compare_release_parts(candidate, specified_parts)
    if operator in ("", "=="):
        return comparison == 0
    if operator == "!=":
        return comparison != 0
    if operator == ">=":
        return comparison >= 0
    if operator == ">":
        return comparison > 0
    if operator == "<=":
        return comparison <= 0
    if operator == "<":
        return comparison < 0
    if operator == "~=":
        if len(specified_parts) < 2:
            raise ConstraintError(f"~={specified_version} needs at least two components")
        return comparison >= 0 and _has_release_prefix(candidate, specified_parts[:-1])

    if operator == "^":
        upper_bound_component = next(
            (index for index, part in enumerate(specified_parts) if part), len(specified_parts) - 1
        )
    else:
        upper_bound_component = min(1, len(specified_parts) - 1)
    upper_bound = specified_parts[:upper_bound_component] + (specified_parts[upper_bound_component] + 1,)
    return comparison >= 0 and _compare_release_parts(candidate, upper_bound) < 0


def satisfies(version: str, constraint: str) -> bool:
    return any(
        all(
            _matches_version_clause(version, operator, specified_version)
            for operator, specified_version in _parse_version_clauses(constraint_group)
        )
        for constraint_group in constraint.split("||")
    )


def newest_accepted(versions: list[str], constraint: str | None) -> str | None:
    compatible_versions = [version for version in versions if constraint is None or satisfies(version, constraint)]
    return max(compatible_versions, key=release_parts, default=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pick the newest Python release the project accepts.")
    parser.add_argument("-p", "--project", default=".", help="project directory (default: cwd)")
    parser.add_argument("versions", nargs="+", help="candidate releases, X.Y.Z")
    args = parser.parse_args(argv)

    try:
        constraint = python_constraint(Path(args.project))
        selected_version = newest_accepted(args.versions, constraint)
    except PyprojectError as error:
        print(f"[update-python] WARNING: {error} — fix it, then re-run", file=sys.stderr)
        return 1
    except ConstraintError as error:
        print(f"[update-python] WARNING: requires-python not understood ({error}) — not applied", file=sys.stderr)
        constraint = None
        selected_version = newest_accepted(args.versions, constraint)

    if selected_version is None:
        print(f"[update-python] WARNING: no stable Python satisfies requires-python {constraint!r}",
              file=sys.stderr)
        return 2
    if constraint is not None:
        print(f"[update-python] requires-python {constraint!r}: newest accepted is {selected_version}", file=sys.stderr)
    print(selected_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
