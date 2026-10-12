"""
tests/run_all.py
-----------------
Runs every offline test suite:   python tests/run_all.py   (from anywhere)
One suite only:                  python tests/run_all.py rules_test

Each suite is a plain script that prints PASS/FAIL lines and exits
non-zero on a failure. Each gets its own empty SQLite database in a
temporary folder, and runs with the Telegram, Anthropic and website
variables removed, so a test can never message a real chat, spend API
credit or touch the hosted database.

NOT run here, because they need something outside this machine:
  live_db_test.py   live market data — only reliable in US market hours
  dash_test.py      a running website (pass its address)
  pg_db_test.py     a Postgres database (DATABASE_URL)
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
NEEDS_OUTSIDE = {"live_db_test", "dash_test", "pg_db_test"}
MASKED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "PUBLIC_BASE_URL")


def main() -> int:
    wanted = sys.argv[1:] or sorted(p.stem for p in HERE.glob("*_test.py") if p.stem not in NEEDS_OUTSIDE)
    failed = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in wanted:
            env = {k: v for k, v in os.environ.items() if k not in MASKED}
            env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8",
                       DATABASE_URL=f"sqlite+aiosqlite:///{pathlib.Path(tmp, name + '.db').as_posix()}")
            args = [sys.executable, str(HERE / f"{name}.py")]
            if name == "event_test":   # takes a database made by an older version, and changes it
                old = pathlib.Path(tmp, "event_old.db")
                shutil.copy(HERE / "fixtures" / "event_old.db", old)
                args.append(old.as_posix())
            run = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            lines = run.stdout.splitlines()
            passed, fails = sum(l.startswith("PASS") for l in lines), [l for l in lines if l.startswith("FAIL")]
            ok = run.returncode == 0 and not fails
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {passed} passed, {len(fails)} failed")
            if not ok:
                failed.append(name)
                print("\n".join(fails + run.stderr.splitlines()[-12:]))
    print("ALL SUITES PASSED" if not failed else f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
