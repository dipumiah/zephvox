"""Machine-readable progress for the desktop app.

Setup and the model downloader print one `ZEPHVOX_EVENT {json}` line per event (to stdout) when asked
with `--progress-json`; anything else on stdout is ordinary log text. The desktop shell and the daemon
read these lines to draw progress bars.

Event types:
    step      {"n", "total", "title"}            setup moved to its next step
    log       {"text"}                           a line setup would have printed
    error     {"message"}                        setup stopped
    download  {"id", "name", "label", "state", "done", "total", "error"}
              state: queued | downloading | verifying | done | failed; sizes in bytes (null if unknown)
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

PREFIX = "ZEPHVOX_EVENT "
_lock = threading.Lock()


def emit(type_: str, /, **data: Any) -> None:
    line = PREFIX + json.dumps({"type": type_, **data}, ensure_ascii=False)
    with _lock:
        print(line, file=sys.stdout, flush=True)


def parse(line: str) -> dict[str, Any] | None:
    """The event on a line of output, or None for ordinary text."""
    if not line.startswith(PREFIX):
        return None
    try:
        event = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    return event if isinstance(event, dict) and "type" in event else None
