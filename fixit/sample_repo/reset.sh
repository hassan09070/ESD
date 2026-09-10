#!/usr/bin/env bash
# Restore sample_repo to its deliberately broken state so every fixit run is repeatable.
set -euo pipefail
cd "$(dirname "$0")"
cat > calculator.py <<'PY'
"""A tiny calculator module used as the fixit sample repo."""


def add(a, b):
    return a + b


def subtract(a, b):
    return b - a  # BUG: arguments swapped


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b + 1  # BUG: off-by-one
PY
rm -rf __pycache__ .pytest_cache
echo "sample_repo reset: 2 of 5 tests failing again"
