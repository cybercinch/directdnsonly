#!/usr/bin/env just --justfile
# directdnsonly — developer task runner
# Requires: just, pyenv, poetry

APP_NAME := "directdnsonly"

# Ensure pyenv shims and common install locations are on PATH so that `python`
# resolves via pyenv (.python-version) and `poetry` is found without a full
# shell init in every recipe.
export PATH := env_var("HOME") + "/.pyenv/shims:" + env_var("HOME") + "/.pyenv/bin:" + env_var("HOME") + "/.local/bin:" + env_var("PATH")

# List available recipes (default)
default:
    @just --list

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

# Install all dependencies (including dev group)
install:
    poetry install

# Install only production dependencies
install-prod:
    poetry install --only main

# Show the Python interpreter that will be used
which-python:
    @poetry run python --version
    @poetry run python -c "import sys; print(sys.executable)"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

# Run the full test suite
test:
    poetry run pytest tests/ -v

# Run tests with terminal coverage report
coverage:
    poetry run pytest tests/ -v --cov=directdnsonly --cov-report=term-missing

# Run tests with HTML coverage report (opens in browser)
coverage-html:
    poetry run pytest tests/ --cov=directdnsonly --cov-report=html
    @echo "Coverage report: htmlcov/index.html"

# Run a single test file or pattern, e.g. just test-one test_reconciler
test-one target:
    poetry run pytest tests/ -v -k "{{target}}"

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

# Format all source and test files with black
fmt:
    poetry run black directdnsonly/ tests/

# Check formatting without making changes (CI-safe)
fmt-check:
    poetry run black --check directdnsonly/ tests/

# CI gate — run fmt-check then test, fail fast
ci: fmt-check test

# Start the application
run:
    poetry run python -m directdnsonly

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# Build a standalone binary with PyInstaller
build:
    poetry run pyinstaller \
        --hidden-import=json \
        --hidden-import=pymysql \
        --hidden-import=cheroot \
        --hidden-import=cheroot.ssl.pyopenssl \
        --hidden-import=cheroot.ssl.builtin \
        --noconfirm --onefile \
        --name=directdnsonly \
        directdnsonly/main.py
    rm -f *.spec

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

# Remove build artefacts, caches, and compiled bytecode
clean:
    rm -rf dist/ build/*.spec .coverage htmlcov/ .pytest_cache/
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -name "*.pyc" -delete 2>/dev/null || true
