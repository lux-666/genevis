"""A local conda run substitute for tests that supply their own tools."""

import shlex
from pathlib import Path


def write_fake_conda(root: Path) -> Path:
    launcher = root / "conda"
    launcher.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        'case "$1" in run|--live-stream|--no-capture-output) shift;; '
        "-p|-n) shift 2;; *) break;; esac\ndone\n"
        + f'export PATH={shlex.quote(str(root / "bin"))}:"$PATH"\nexec "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher
