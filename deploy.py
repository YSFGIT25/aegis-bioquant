"""
deploy.py
----------
Deploys one Railway service:   python deploy.py scanner   |   python deploy.py web

Both services are built from this folder with the same Dockerfile; what
each one does is decided by its AEGIS_ROLE variable on Railway (see
aegis_bioquant/entrypoint.py), so there is nothing service-specific to
upload.

WHY NOT JUST `railway up`: the Railway project and environment are named
explicitly here instead of relying on the folder being "linked". The CLI
keys a link by the folder's exact path spelling; run from the same
folder spelled differently (Desktop vs desktop) it finds no link and
silently creates a brand-new project to deploy into. That happened once.
"""

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SERVICES = ("scanner", "web")
PROJECT_ID = "259caee5-561f-4e92-be5c-b06a330b5835"  # aegis-bioquant
ENVIRONMENT = "production"


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in SERVICES:
        print(f"usage: python deploy.py [{' | '.join(SERVICES)}]")
        return 2
    railway = shutil.which("railway")
    if railway is None:
        print("The Railway CLI is not installed (npm install -g @railway/cli).")
        return 1
    return subprocess.call(
        [railway, "up", "--project", PROJECT_ID, "--environment", ENVIRONMENT,
         "--service", sys.argv[1], "--ci"],
        cwd=ROOT,
    )


if __name__ == "__main__":
    sys.exit(main())
