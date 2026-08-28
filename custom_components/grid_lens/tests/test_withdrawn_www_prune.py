#!/usr/bin/env python3
"""Withdrawn www/ files are pruned before the static path is exposed.

An in-place update (HACS, or sync-to-ha.sh's `cp -r`) copies files in and never
deletes, so removing a file from the repo does not remove it from an existing
install. `/grid_lens` is registered as a static path over the whole www/ tree, so
a withdrawn file keeps being served.

That bit for real: grid-lens-powerflow-card.js was moved to gridlens-api behind
PowerflowIconView's entitlement check, but installs that updated across that
commit kept the pre-gating copy at /grid_lens/cards/grid-lens-powerflow-card.js
— outside the gate. Found 2026-08-28 on the dev rig, still dated Aug 2.

Home Assistant is not importable here, so the two module-level names are compiled
out of __init__.py by AST rather than imported.

Run:  python3 tests/test_withdrawn_www_prune.py
"""
from __future__ import annotations

import ast
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
_fails: list[str] = []


def _load():
    src = (ROOT / "__init__.py").read_text()
    tree = ast.parse(src)
    want_fn, want_const = "_prune_withdrawn_www_files", "_WITHDRAWN_WWW_FILES"
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name == want_fn)
             or (isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == want_const for t in n.targets))]
    assert len(nodes) == 2, f"expected {want_fn} + {want_const} at module level"
    ns: dict = {}
    import logging
    ns["_LOGGER"] = logging.getLogger("test")
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<prune>", "exec"), ns)
    return ns[want_fn], ns[want_const]


prune, WITHDRAWN = _load()


def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond:
        _fails.append(label)


def test_removes_the_withdrawn_file():
    print("test_removes_the_withdrawn_file:")
    with tempfile.TemporaryDirectory() as d:
        www = pathlib.Path(d) / "www"
        (www / "cards").mkdir(parents=True)
        stale = www / "cards" / "grid-lens-powerflow-card.js"
        keep = www / "cards" / "grid-lens-card.js"
        stale.write_text("// pre-gating copy")
        keep.write_text("// still shipped")
        prune(www)
        check("withdrawn file is gone", not stale.exists())
        check("a shipped card is untouched", keep.exists())


def test_is_a_noop_on_a_clean_install():
    print("test_is_a_noop_on_a_clean_install:")
    with tempfile.TemporaryDirectory() as d:
        www = pathlib.Path(d) / "www"
        (www / "cards").mkdir(parents=True)
        prune(www)
        check("no crash when nothing to prune", True)
    check("no crash when www/ does not exist at all",
          prune(pathlib.Path(d) / "gone") is None)


def test_never_escapes_www():
    """The tuple drives an unlink() on the user's filesystem, so a traversal entry
    must be refused rather than trusted."""
    print("test_never_escapes_www:")
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        www = root / "www"
        www.mkdir()
        outside = root / "configuration.yaml"
        outside.write_text("do not touch")
        g = prune.__globals__
        saved = g["_WITHDRAWN_WWW_FILES"]
        try:
            g["_WITHDRAWN_WWW_FILES"] = ("../configuration.yaml",)
            prune(www)
            check("a traversal entry cannot delete outside www/", outside.exists())
        finally:
            g["_WITHDRAWN_WWW_FILES"] = saved


def test_entries_are_plain_relative_filenames():
    print("test_entries_are_plain_relative_filenames:")
    ok = all(
        isinstance(e, str) and e and not e.startswith(("/", "~"))
        and ".." not in e.split("/") and "*" not in e and "?" not in e
        for e in WITHDRAWN
    )
    check("no globs, absolute paths or traversal in the list", ok)
    check("the gated Power Flow card is listed",
          "cards/grid-lens-powerflow-card.js" in WITHDRAWN)


if __name__ == "__main__":
    test_removes_the_withdrawn_file()
    test_is_a_noop_on_a_clean_install()
    test_never_escapes_www()
    test_entries_are_plain_relative_filenames()
    if _fails:
        sys.exit(f"\nFAILED: {len(_fails)} check(s): {_fails}")
    print("\nOK — withdrawn www/ files are pruned, safely.")
