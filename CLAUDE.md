Instructions

Only make code changes that are explicitly requested

If you notice something else that might need attention, ask first and wait for approval before making any changes

Do not overstep the scope of what was asked

NEVER make file edits, run commands (docker build, docker run, flutter build, etc.), or execute any actions without first presenting what you plan to do in plain English and waiting for explicit user approval.

Once a command or change is approved and executed, automatically do the git commit and push — git commit/push do NOT need separate approval.

If you notice uncommitted changes made by the user (modified files, new untracked files, or deleted files), commit and push those as well. The only exceptions are files that match .gitignore patterns.

Do not include "Co-Authored-By" lines in commit messages

When debugging issues, take this approach:   1.  Check your own code changes first from the time it was working.  2.  Next, check for environmental causes first (concurrent processes, file locking, resource contention, disk space, memory, network, etc).  3.  If working with LinkedIn, and only as a last resort, consider changes with LinkedIn's rules because that can be hardest to troubleshoot



Testing Policy

Unless i state otherwise, do the following where applicable:

1. Every bug fix or feature must be accompanied by at least one unit test that would have caught the bug or verifies the new behavior

2\.  Backend: add tests to the relevant file in backend/tests/ and run with:  python -m pytest backend/tests/<test\_file>.py -v

3\.  Flutter: add widget/unit tests in mobile\_flutter/test/ and run with:

4\.  cd mobile\_flutter \&\& flutter test

5\.  Run the full suite (pytest backend/tests/) before committing if changes touch shared code (models, services, routers)

6\.  Do not commit if any tests fail — fix the failure first

7\.  For Flutter UI changes, at minimum verify with flutter analyze before committing; note any behaviors that can only be confirmed visually

If i tell you to stop, do so



