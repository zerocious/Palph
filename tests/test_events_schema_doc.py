"""
Drift test for [docs/events_schema.md](../docs/events_schema.md) —
PA-roadmap #4.

Walks bot.py and services.py looking for `event_repo.log(...)` (and the
matching `log_deploy_event` wrapper) calls, extracts the event_name string
literal, and asserts each one has a `#### <name>` heading in
docs/events_schema.md.

Catches the common drift case: someone adds a new event hook and forgets
to document it. The opposite drift (documented but unused) is allowed —
documenting future events ahead of implementation is fine.

If you intentionally want to log an event without documenting it (e.g.,
debug-only), add the name to `_DOCS_UNDOCUMENTED_WHITELIST` with a
comment explaining why.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).parent.parent
_SCAN_TARGETS = [_ROOT / "bot.py", _ROOT / "services.py"]
_DOC_FILE = _ROOT / "docs" / "events_schema.md"

# Events that may exist in code but don't need documentation here.
# Keep this list short — prefer documenting events properly.
# (Empty for now; using `set()` not `{}` because `{}` is a dict literal.)
_DOCS_UNDOCUMENTED_WHITELIST: set[str] = set()

# Hard-coded wrapper functions that emit a specific event name internally.
# Maps the wrapper's qualified name (as it appears in source) → the
# event_name it actually emits. We scan calls to these wrappers and treat
# them as if they were `event_repo.log(..., "<name>", ...)` directly.
_WRAPPER_EMITTERS: dict[str, str] = {
    "log_deploy_event": "system.deploy",
}


def _extract_event_names_from_source(path: Path) -> set[str]:
    """
    Parse `path` and return the set of event_name string literals passed
    to:
      - event_repo.log("<name>", ...)  (positional name as 2nd arg after user_id)
      - <wrapper>(...)  for wrappers known to emit a fixed event
    Skips f-strings and variable names — only string literals count as
    "documented".
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # Pattern A: event_repo.log(user_id, "event_name", {...})
        # or any method named .log() where the second positional arg is
        # a string literal. We scope to .log so we don't grab logger.info etc.
        if isinstance(func, ast.Attribute) and func.attr == "log":
            # event_repo.log(...) / self.event_repo.log(...) / self._events.log(...)
            # We don't try to disambiguate which `.log` — narrowing to second
            # positional arg being a string literal is enough.
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                val = node.args[1].value
                if isinstance(val, str):
                    found.add(val)

        # Pattern B: known wrappers like log_deploy_event(event_repo, ...).
        if isinstance(func, ast.Name) and func.id in _WRAPPER_EMITTERS:
            found.add(_WRAPPER_EMITTERS[func.id])
        if isinstance(func, ast.Attribute) and func.attr in _WRAPPER_EMITTERS:
            found.add(_WRAPPER_EMITTERS[func.attr])

    return found


def _documented_event_names() -> set[str]:
    """
    Parse docs/events_schema.md and return the set of event names
    documented by an `#### <name>` heading (level 4).
    """
    text = _DOC_FILE.read_text(encoding="utf-8")
    # Headings look like:    #### `event_name`    OR    #### event_name
    pattern = re.compile(r"^####\s+`?([\w.]+)`?\s*$", re.MULTILINE)
    return set(pattern.findall(text))


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_doc_file_exists():
    assert _DOC_FILE.is_file(), f"missing {_DOC_FILE}"


def test_no_undocumented_events():
    """
    Every event_name fired in bot.py / services.py must appear as an
    `#### <name>` heading in docs/events_schema.md.
    """
    fired: set[str] = set()
    for path in _SCAN_TARGETS:
        fired |= _extract_event_names_from_source(path)
    fired -= _DOCS_UNDOCUMENTED_WHITELIST

    documented = _documented_event_names()
    missing = fired - documented
    assert not missing, (
        f"Event(s) fired in code but missing from docs/events_schema.md: "
        f"{sorted(missing)}. Add an `#### <name>` section to the doc, or "
        f"whitelist with rationale in _DOCS_UNDOCUMENTED_WHITELIST."
    )


def test_at_least_one_event_documented():
    """Sanity: parser actually finds events. Guards against regex breakage."""
    assert len(_documented_event_names()) >= 5


def test_at_least_one_event_in_code():
    """Sanity: AST walker actually finds events. Guards against AST changes."""
    fired: set[str] = set()
    for path in _SCAN_TARGETS:
        fired |= _extract_event_names_from_source(path)
    assert len(fired) >= 5


def test_system_deploy_documented():
    """Explicit check: PA-roadmap #6's marker event is documented."""
    assert "system.deploy" in _documented_event_names()


def test_experiment_assigned_documented():
    """Explicit check: PA-roadmap #1's assignment event is documented."""
    assert "experiment.assigned" in _documented_event_names()
