#!/usr/bin/env python3
"""Tiny zero-dependency runner for this repository's plain test functions."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent


def load_module(path: Path):
    name = f"codex_tests_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    passed = 0
    failed = 0
    for path in sorted(TEST_DIR.glob("test_*.py")):
        module = load_module(path)
        tests = [
            (name, fn)
            for name, fn in inspect.getmembers(module, inspect.isfunction)
            if name.startswith("test_") and not inspect.signature(fn).parameters
        ]
        for name, fn in tests:
            label = f"{path.name}::{name}"
            try:
                fn()
            except Exception:
                failed += 1
                print(f"FAIL {label}")
                traceback.print_exc()
            else:
                passed += 1
                print(f"PASS {label}")
    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
