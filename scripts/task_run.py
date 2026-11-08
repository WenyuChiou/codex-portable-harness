"""Run one foreground autonomous command with a three-job limit and owned scratch."""
import sys
from test_run import main

if __name__ == "__main__":
    sys.argv.insert(1, "--light")
    raise SystemExit(main())
