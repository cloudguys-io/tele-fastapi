"""Generate the endpoint methods of async_client.py from client.py.

uv run python scripts/gen_async.py          # rewrite async_client.py
uv run python scripts/gen_async.py --check  # exit 1 if it is out of date (CI and tests)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "src" / "tele_fastapi"
START, END = "    # ---- endpoints ----\n", "    # ---- end endpoints ----\n"


def block(text: str) -> tuple[str, str, str]:
    head, rest = text.split(START, 1)
    mid, tail = rest.split(END, 1)
    return head, mid, tail


def to_async(sync_block: str) -> str:
    out = re.sub(r"^    def ", "    async def ", sync_block, flags=re.M)
    out = out.replace("self._send(", "await self._send(")
    return out


def generate() -> str:
    _, sync_mid, _ = block((PKG / "client.py").read_text())
    head, _, tail = block((PKG / "async_client.py").read_text())
    text = head + START + to_async(sync_mid) + END + tail
    return subprocess.run(
        ["ruff", "format", "--stdin-filename", "async_client.py", "-"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        cwd=PKG.parent.parent,
    ).stdout


def main() -> int:
    target = PKG / "async_client.py"
    want = generate()
    if "--check" in sys.argv:
        if target.read_text() != want:
            print("async_client.py is out of date: run scripts/gen_async.py", file=sys.stderr)
            return 1
        return 0
    target.write_text(want)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
