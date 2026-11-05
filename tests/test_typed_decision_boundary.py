"""Keep typed-decision imports independent of JiuwenSwarm internals."""

import ast
from pathlib import Path

from belle_agent import typed_decision

PACKAGE = "belle_agent.typed_decision"
PACKAGE_ROOT = Path(typed_decision.__file__).parent


def _modules() -> list[Path]:
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert paths, f"no modules found under {PACKAGE_ROOT}"
    return paths


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add(PACKAGE)
            elif node.module:
                names.add(node.module)
    return names


def _offending(names: set[str]) -> set[str]:
    offenders = set()
    for name in names:
        if name == PACKAGE or name.startswith(f"{PACKAGE}."):
            continue
        if name == "jiuwenswarm" or name.startswith("jiuwenswarm."):
            offenders.add(name)
    return offenders


def test_the_package_imports_nothing_else_from_jiuwenswarm() -> None:
    """Keep the package independent of JiuwenSwarm internals."""
    found: dict[str, set[str]] = {}
    for path in _modules():
        offenders = _offending(_imported_names(path))
        if offenders:
            found[str(path.relative_to(PACKAGE_ROOT))] = offenders
    assert not found, (
        f"typed_decision must not import the rest of jiuwenswarm; a move would have to rewrite these: {found}"
    )


def test_the_tests_import_nothing_else_from_jiuwenswarm() -> None:
    """Keep test fixtures independent of JiuwenSwarm internals."""
    here = Path(__file__).parent
    suites = sorted(here.glob("test_typed_decision_*.py"))
    assert suites, "the protocol's own suites were not found"
    found: dict[str, set[str]] = {}
    for path in suites:
        offenders = _offending(_imported_names(path))
        if offenders:
            found[path.name] = offenders
    assert not found, f"the typed_decision suites must import only the package itself: {found}"
