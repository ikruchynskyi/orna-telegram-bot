#!/usr/bin/env bash
# Native install (macOS/Homebrew or Debian/Ubuntu). For a container instead,
# see docker-compose.yml - that needs nothing on the host but Docker.
#
#   ./install.sh            full install
#   ./install.sh --no-deps  skip system packages (venv + python deps + DB only)
#
# Idempotent: safe to re-run after a pull.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"
VENV="$REPO/.venv"
SKIP_SYS=0
[[ "${1:-}" == "--no-deps" ]] && SKIP_SYS=1

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- system deps
# tesseract needs the UKRAINIAN language data, not just the binary: the OCR
# runs with lang="eng+ukr" and degrades to English-only without it, which
# shows up as "the bot ignored my screenshot" rather than as an error.
if [[ $SKIP_SYS -eq 0 ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    command -v brew >/dev/null || die "Homebrew not found - install it or re-run with --no-deps"
    say "Installing system packages with Homebrew"
    brew install tesseract tesseract-lang ffmpeg yt-dlp || warn "brew install reported a problem - continuing"
  elif command -v apt-get >/dev/null; then
    say "Installing system packages with apt"
    SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
    $SUDO apt-get update
    $SUDO apt-get install -y python3 python3-venv python3-pip \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr ffmpeg
  else
    warn "Unknown platform - install tesseract (+ eng/ukr data), ffmpeg and yt-dlp yourself, then re-run with --no-deps"
  fi
fi

# ----------------------------------------------------------------- python env
command -v python3 >/dev/null || die "python3 not found"
say "Python venv at $VENV"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt
echo "installed: $("$VENV/bin/python" -V)"

# --------------------------------------------------------------------- config
# Never generated with real values and never overwritten - a clobbered .env
# here would mean a bot token lost from a working install.
if [[ ! -f .env ]]; then
  if [[ -f .env.dist ]]; then
    cp .env.dist .env
    warn "Created .env from .env.dist - fill in BOT_TOKEN, SHEETS_API_KEY, SPREADSHEET_ID before starting"
  else
    warn "No .env and no .env.dist - the bot will not start without BOT_TOKEN/SHEETS_API_KEY/SPREADSHEET_ID"
  fi
else
  missing=""
  for key in BOT_TOKEN SHEETS_API_KEY SPREADSHEET_ID; do
    grep -qE "^${key}=.+" .env || missing="$missing $key"
  done
  [[ -n "$missing" ]] && warn ".env is missing a value for:$missing"
fi

# ------------------------------------------------------------------ codex DB
# Built from the cached aussiescodex dump; fetches it first if there is no
# cache yet. Not fatal - connect() rebuilds on first use if this is skipped
# (e.g. no network at install time).
say "Building the codex database"
if ! ( set -a; [[ -f .env ]] && . ./.env; set +a; "$VENV/bin/python" orna_codex_db.py ); then
  warn "Codex DB build failed (no network?) - it will be built on first use"
fi

# ------------------------------------------------------------------- verify
say "Verifying the install"
( set -a; [[ -f .env ]] && . ./.env; set +a
  "$VENV/bin/python" - <<'PY'
import shutil, subprocess, sys
ok = True
for mod in ("telegram", "httpx", "bs4", "pytesseract", "PIL", "dotenv"):
    try:
        __import__(mod)
    except Exception as e:
        print(f"  MISSING python module {mod}: {e}"); ok = False
tess = shutil.which("tesseract")
if tess:
    langs = subprocess.run([tess, "--list-langs"], capture_output=True, text=True).stdout
    for want in ("eng", "ukr"):
        if want not in langs:
            print(f"  tesseract is missing the {want!r} language data (OCR will degrade)"); ok = False
else:
    print("  tesseract not found - screenshot OCR will not work"); ok = False
for binary in ("ffmpeg", "ffprobe"):
    if not shutil.which(binary):
        print(f"  {binary} not found - /go's video pipeline will not work")
# yt-dlp is called by ABSOLUTE path (YTDLP_PATH), whose default is a macOS
# standalone download - so finding it on PATH is not enough, the env var has
# to point at it or /go silently fails to download anything.
import os
ytdlp = os.environ.get("YTDLP_PATH") or os.path.expanduser("~/yt-dlp_macos")
if not os.path.exists(ytdlp):
    found = shutil.which("yt-dlp")
    if found:
        print(f"  add this to .env so /go can find yt-dlp:  YTDLP_PATH={found}")
    else:
        print("  yt-dlp not found - /go's video pipeline will not work")
try:
    import orna_codex_db as db
    n = db.run_sql("SELECT count(*) FROM records")["rows"][0][0]
    print(f"  codex database: {n} records")
    if n < 1000:
        print("  codex database looks short"); ok = False
except Exception as e:
    print(f"  codex database unavailable: {e}"); ok = False
print("\nREADY" if ok else "\nINSTALLED WITH WARNINGS (see above)")
sys.exit(0)
PY
)

cat <<EOF

Start it:   $VENV/bin/python telegram_bot.py
Self-check: $VENV/bin/python orna_test_suite.py
Docker:     docker compose up -d
EOF
