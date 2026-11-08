"""Run a bounded autonomous command after rejecting the account's home Git repo."""
import json
import os
from pathlib import Path
import subprocess
import sys
from test_run import main as command_main


def require_project_repository(cwd=None, account_home=None):
    cwd = Path.cwd() if cwd is None else Path(cwd)
    account_home = Path.home() if account_home is None else Path(account_home)
    git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    git_env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(['git', '-c', 'core.quotepath=false', 'rev-parse', '--show-toplevel'], cwd=cwd,
                            capture_output=True, text=True, encoding="utf-8", timeout=10,
                            env=git_env)
    if result.returncode:
        raise RuntimeError('REPOSITORY_CHECK_FAILED: ' + result.stderr.strip())
    root = Path(os.path.abspath(result.stdout.strip()))
    if root == Path(os.path.abspath(account_home)):
        raise RuntimeError('HOME_REPOSITORY_BLOCKED: ' + str(root) + '; stop and select a project checkout')
    return root


def main():
    try:
        root = require_project_repository()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 78
    print(json.dumps({'repository_root': str(root), 'check': 'git rev-parse --show-toplevel'}), flush=True)
    if sys.argv[1:] == ['--check-repo']:
        return 0
    sys.argv.insert(1, '--light')
    return command_main()


if __name__ == '__main__':
    raise SystemExit(main())
