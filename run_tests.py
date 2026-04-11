#!/usr/bin/env python
"""
Interactive test runner for TogetherMindsAI.
Run with: python run_tests.py
Prompts for full or partial suite, then executes pytest.
Loops until you quit.
"""
import subprocess
import sys
import os
from glob import glob

VENV_PYTHON = os.path.join(os.path.dirname(__file__),
                            "TogetherMindsAI.venv", "Scripts", "python.exe")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def discover_test_files():
    files = sorted(glob("tests/test_*.py"))
    return files


def prompt_choice(prompt, options):
    """Print numbered options and return the chosen value."""
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")
    while True:
        raw = input(prompt).strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print(f"  Enter a number between 1 and {len(options)}, or q to quit.")


def run_pytest(args):
    cmd = [PYTHON, "-m", "pytest"] + args + ["-v"]
    print("\n" + "-" * 60)
    print("Running: " + " ".join(cmd))
    print("-" * 60 + "\n", flush=True)
    result = subprocess.run(cmd)
    print("\n" + "-" * 60, flush=True)
    return result.returncode


def main():
    print("\n==============================")
    print("  TogetherMindsAI Test Runner")
    print("==============================\n")

    test_files = discover_test_files()

    while True:
        print("What would you like to run?\n")
        suite_options = ["Full suite", "Partial suite (pick files)", "Quit"]
        choice = prompt_choice("Choice: ", suite_options)

        if choice is None or choice == "Quit":
            print("Bye.")
            break

        if choice == "Full suite":
            run_pytest(["tests/"])

        elif choice == "Partial suite (pick files)":
            print("\nAvailable test files (space-separated numbers, e.g. 1 3):\n")
            for i, f in enumerate(test_files, 1):
                print(f"  {i}) {f}")
            print()

            while True:
                raw = input("Select files: ").strip()
                if raw.lower() in ("q", "quit", "exit"):
                    break
                parts = raw.split()
                selected = []
                valid = True
                for p in parts:
                    if p.isdigit() and 1 <= int(p) <= len(test_files):
                        selected.append(test_files[int(p) - 1])
                    else:
                        print(f"  '{p}' is not a valid number. Try again.")
                        valid = False
                        break
                if valid and selected:
                    run_pytest(selected)
                    break

        print()
        again = input("Run again? [Y/n]: ").strip().lower()
        if again in ("n", "no", "q", "quit"):
            print("Bye.")
            break
        print()


if __name__ == "__main__":
    main()
