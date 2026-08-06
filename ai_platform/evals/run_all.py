"""
Run all three eval suites (RCA, runbook, copilot tool-use) back to back and
print a combined scorecard. Exits non-zero if any suite has a failing
fixture.

    uv run python -m ai_platform.evals.run_all
"""

import sys

from ai_platform.evals import run_rca_evals, run_runbook_evals, run_tool_use_evals


def main() -> int:
    rca_rc = run_rca_evals.main()
    runbook_rc = run_runbook_evals.main()
    tool_use_rc = run_tool_use_evals.main()

    print("\n=== Combined result ===")
    for name, rc in [("RCA", rca_rc), ("Runbook", runbook_rc), ("Tool-use", tool_use_rc)]:
        print(f"  {name}: {'PASS' if rc == 0 else 'FAIL'}")

    return 0 if all(rc == 0 for rc in (rca_rc, runbook_rc, tool_use_rc)) else 1


if __name__ == "__main__":
    sys.exit(main())
