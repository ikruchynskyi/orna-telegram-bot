#!/usr/bin/env python3
"""
orna_loop_harness.py - drive the /orna ReAct loop end-to-end against the
REAL local/cloud Ollama and REAL codex/aussies/sheets data, with a FAKE
Telegram message, so you can see exactly what a request would send WITHOUT
a live chat or bot restart.

This is the "no-mocks" verification harness the repo's debugging approach
relies on. See ../SKILL.md for the full method. It prints, per run:
  * the ordered ReAct ACTION trace - which tool the model picked each turn,
    with its args (this is how you see routing: class_guide vs query vs
    search_codex vs ...); and
  * every USER-VISIBLE reply the tools posted - search-result headers,
    entry-card button labels, and the final finish() text.

Why this shape: /orna is thin glue over three live, unmocked services, so
the only faithful test is the real loop against the real data. A FakeMessage
whose reply_* methods just record instead of calling Telegram lets you run
that loop from a throwaway script. Reading the ACTION trace tells you WHICH
layer misbehaved (see SKILL.md's "localize the layer" step).

Usage (from the repo root, with the bot's env loaded):

    set -a && source .env && set +a         # loads SHEETS_API_KEY etc.

    # One run against the real prod path (cloud model, local fallback):
    Q="show codex items for raid heretic using omniflask build" \
        python3 .claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py

    # Model output is non-deterministic - verify a fix over SEVERAL runs,
    # never one. N repeats the same request:
    Q="балор меч" N=5 python3 .../orna_loop_harness.py

    # Force local-only (skip the cloud attempt entirely - faster, and works
    # with no OLLAMA_API_KEY; but test the CLOUD path too before shipping,
    # since that is what real users hit for the first CLOUD_STEPS turns):
    Q="..." FORCE_LOCAL=1 python3 .../orna_loop_harness.py

Environment:
  * SHEETS_API_KEY  - REQUIRED just to import the bot's modules.
  * OLLAMA_API_KEY  - enables the cloud model (real prod path). Without it,
                      the cloud attempt fails and falls back to local.
  * TAVILY_API_KEY  - only needed if the request triggers web_search.
  * Q               - the request text (what the user would type after /orna).
  * N               - repeat count (default 1).
  * FORCE_LOCAL=1   - skip cloud, local Ollama only.
  * ORNA_REPO_ROOT  - repo path (defaults to the current directory).

NEVER hardcode API tokens in this file or commit them - pass them via the
environment. The repo's .env is gitignored for exactly this reason.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

REPO_ROOT = os.environ.get("ORNA_REPO_ROOT") or os.getcwd()
sys.path.insert(0, REPO_ROOT)

if not os.environ.get("SHEETS_API_KEY"):
    sys.exit("SHEETS_API_KEY is not set - run `set -a && source .env && set +a` first "
             "(it is read at import time by orna_sheets, so the module won't even load without it).")

import telegram_orna as T  # noqa: E402  (import after sys.path/env setup)

if os.environ.get("FORCE_LOCAL") == "1":
    # CLOUD_STEPS gates how many opening turns may try the cloud model; 0
    # means every turn goes straight to local Ollama.
    T.CLOUD_STEPS = 0


class FakeMessage:
    """Stand-in for a telegram.Message that records replies instead of
    sending them. __getattr__ turns ANY method call (reply_text,
    reply_photo, reply_markdown, ...) into an async no-op that appends
    (method_name, text_or_caption, [button labels]) to a shared sink and
    returns another FakeMessage so chained sends work."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def __getattr__(self, name: str):
        async def _record(*args, **kwargs):
            text = kwargs.get("text") or kwargs.get("caption") or (args[0] if args else "") or ""
            buttons: list = []
            markup = kwargs.get("reply_markup")
            if markup is not None:
                try:
                    for row in markup.inline_keyboard:
                        buttons.extend(b.text for b in row)
                except Exception:
                    pass
            self._sink.append((name, str(text), buttons))
            return FakeMessage(self._sink)
        return _record


async def run_once(query: str) -> None:
    replies: list = []
    messages = [
        {"role": "system", "content": T._orna_system_prompt(query)},
        {"role": "user", "content": query},
    ]
    sid = T._new_orna_session(messages, T.MAX_STEPS)
    await T._advance(sid, FakeMessage(replies))

    print("\n-- ACTION TRACE (which tool the model picked each turn) --")
    for msg in T._ORNA_SESSIONS[sid].messages:
        if msg.get("role") == "assistant":
            try:
                step = json.loads(msg["content"])
            except Exception:
                continue
            print(f"  {step.get('action')} :: input={str(step.get('action_input'))[:70]!r} "
                  f"args={json.dumps(step.get('args') or {})[:150]}")

    print("\n-- USER-VISIBLE REPLIES (tool cards + final finish text) --")
    for name, text, buttons in replies:
        print(f"  [{name}] {text.replace(chr(10), ' ')[:300]}")
        if buttons:
            print(f"       buttons: {buttons}")


async def main() -> None:
    query = os.environ.get("Q")
    if not query:
        sys.exit('Set Q to the request text, e.g. Q="balor sword" python3 orna_loop_harness.py')
    n = int(os.environ.get("N", "1"))
    mode = "LOCAL only" if os.environ.get("FORCE_LOCAL") == "1" else "CLOUD -> local fallback"
    for i in range(1, n + 1):
        print(f"\n{'=' * 72}\nRUN {i}/{n}  |  mode={mode}  |  Q={query!r}\n{'=' * 72}")
        try:
            await run_once(query)
        except Exception as exc:  # a harness crash shouldn't hide earlier runs' output
            print(f"RUN {i} raised: {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())
