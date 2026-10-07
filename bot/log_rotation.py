"""Monthly log cleanup, so the launchd logs never fill the disk.

launchd appends the bot's output to telegrambot.log / telegrambot_error.log in
the repo root, forever (the error log grew to 2MB in three days of testing).
On the 1st of every month this job:
  1. gzips each log into data/cache/logs/<name>.<YYYY-MM>.gz (the month it covers),
  2. truncates the live file IN PLACE - launchd holds it open in append mode,
     so it keeps writing at the new end: no restart, no lost lines,
  3. keeps the newest KEEP_MONTHS archives and deletes older ones.
Run by the bot's job queue (telegram_bot.py); `python3 -m bot.log_rotation`
runs the self-check.
"""
from __future__ import annotations

import datetime
import gzip
import logging
import os
import shutil
from pathlib import Path

import paths

logger = logging.getLogger(__name__)

LOGS = [paths.ROOT / "telegrambot.log", paths.ROOT / "telegrambot_error.log"]
ARCHIVE_DIR = paths.CACHE / "logs"
KEEP_MONTHS = 3


def rotate(now: datetime.datetime, logs: list = LOGS, archive_dir: Path = ARCHIVE_DIR,
           keep: int = KEEP_MONTHS) -> list:
    """Archive and truncate each non-empty log; returns the archives written."""
    month = (now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")   # the month just ended
    archive_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for log in logs:
        if not log.exists() or log.stat().st_size == 0:
            continue
        target = archive_dir / f"{log.name}.{month}.gz"
        tmp = target.with_suffix(".tmp")
        with open(log, "rb") as src, gzip.open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, target)
        # Truncate, never delete/recreate: launchd keeps the file open, and a new
        # file would never be written to.
        with open(log, "r+b") as fh:
            fh.truncate(0)
        written.append(target)
        old = sorted(archive_dir.glob(f"{log.name}.*.gz"))[:-keep]
        for p in old:
            p.unlink()
    return written


async def job(context) -> None:
    try:
        done = rotate(datetime.datetime.now())
        logger.info("log rotation: archived %s", [p.name for p in done])
    except Exception:
        logger.warning("log rotation failed - logs left as they are", exc_info=True)


def _demo() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, empty = tmp / "bot.log", tmp / "empty.log"
        log.write_text("line 1\nline 2\n")
        empty.write_text("")
        with open(log, "ab") as appender:          # stands in for launchd's open handle
            for month in (7, 8, 9, 10, 11):
                log.write_text(f"month {month}\n") if month > 7 else None
                written = rotate(datetime.datetime(2026, month, 1, 4), [log, empty], tmp / "arch", keep=3)
            appender.write(b"after\n")
            appender.flush()
        names = sorted(p.name for p in (tmp / "arch").glob("*.gz"))
        assert names == ["bot.log.2026-08.gz", "bot.log.2026-09.gz", "bot.log.2026-10.gz"], names
        assert gzip.open(tmp / "arch" / "bot.log.2026-10.gz").read() == b"month 11\n"
        assert written and log.read_bytes() == b"after\n", log.read_bytes()   # truncated, writer continues
        assert not any("empty" in n for n in names)                           # empty logs are skipped
    print("log_rotation: _demo ok")


if __name__ == "__main__":
    _demo()
