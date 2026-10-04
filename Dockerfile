# Runs the whole bot anywhere Docker runs. The three system binaries are the
# reason this image exists at all - `pip install` cannot give you tesseract
# (with UKRAINIAN traineddata, which the amity/offerings OCR needs) or ffmpeg,
# and CLAUDE.md records several rounds of "video but no audio" bugs that were
# really just a minimal PATH not finding them. Here they are at fixed absolute
# paths, passed to the code by env var, so there is nothing to discover.
FROM python:3.11-slim

# tesseract-ocr-ukr is NOT optional: telegram_assess OCRs with lang="eng+ukr"
# and silently falls back to eng-only when the data is missing, which turns
# into "the bot ignored my screenshot" rather than an error.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr \
        ffmpeg \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before the source, so editing a .py does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Absolute paths, because the code's own fallbacks are Homebrew/macOS layouts
# that do not exist here. TESSDATA_PREFIX is set explicitly for the same
# reason telegram_assess derives it at runtime on macOS.
ENV PYTHONUNBUFFERED=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    FFMPEG_PATH=/usr/bin/ffmpeg \
    FFPROBE_PATH=/usr/bin/ffprobe \
    YTDLP_PATH=/usr/local/bin/yt-dlp \
    OLLAMA_HOST=http://host.docker.internal:11434

# Fail the BUILD rather than the first user request if a system dependency is
# missing or the OCR language data did not land.
RUN python -c "import os, subprocess, sqlite3; \
      import telegram, httpx, bs4, pytesseract, PIL, dotenv, apscheduler; \
      from telegram.ext import JobQueue; \
      assert sqlite3.sqlite_version_info >= (3, 9), sqlite3.sqlite_version; \
      sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(a)'); \
      p = os.environ['TESSDATA_PREFIX']; \
      assert os.path.isdir(p), 'TESSDATA_PREFIX does not exist in this image: ' + p; \
      langs = subprocess.run(['tesseract','--list-langs'], capture_output=True, text=True).stdout; \
      assert 'ukr' in langs and 'eng' in langs, langs; \
      subprocess.run(['/usr/bin/ffmpeg','-version'], check=True, capture_output=True); \
      subprocess.run(['/usr/local/bin/yt-dlp','--version'], check=True, capture_output=True); \
      print('image deps ok')"

CMD ["python", "telegram_bot.py"]
