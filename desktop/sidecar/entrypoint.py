import os
import shlex
import sys

from worldgs_receiver.cli import main


def _argv_with_local_training_env(argv: list[str]) -> list[str]:
    command = os.environ.get("WORLDGS_LOCAL_TRAINING_COMMAND", "").strip()
    cwd = os.environ.get("WORLDGS_LOCAL_TRAINING_CWD", "").strip()
    if not command:
        return argv
    result = list(argv)
    for token in shlex.split(command):
        result.append(f"--local-training-command={token}")
    if cwd:
        result.append(f"--local-training-cwd={cwd}")
    return result


if __name__ == "__main__":
    sys.argv = _argv_with_local_training_env(sys.argv)
    main()
