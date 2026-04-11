#!/usr/bin/env python
"""
Auto test runner for TogetherMindsAI.
Run with: python run_tests.py

Watches the project for file changes and automatically reruns the full
test suite on every save. Press Ctrl+C to stop.
"""
import subprocess
import sys
import os

VENV_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "TogetherMindsAI.venv", "Scripts", "python.exe")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable

print("==============================")
print("  TogetherMindsAI Test Runner")
print("  Watching for changes...")
print("  Press Ctrl+C to stop.")
print("==============================\n")

subprocess.run([
    PYTHON, "-m", "pytest_watch",
    "--ignore", "TogetherMindsAI.venv",
    "--ignore", "instance",
    "--ignore", ".git",
    "tests/",
    "--", "-v"
])
