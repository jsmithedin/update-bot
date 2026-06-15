#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["requests"]
# ///
"""Daily Arch/AUR update check — sends a Telegram message if updates are pending."""

import os
import subprocess
import sys

import requests

TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_LEN = 3500
CALLBACK_DATA = "sysupdate:run"


def run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        parts = [s for s in (result.stderr.strip(), result.stdout.strip()) if s]
        detail = "\n".join(parts) if parts else "(no output)"
        msg = f"{cmd[0]} failed (exit {result.returncode}): {detail}"
        if cmd[0] == "checkupdates" and result.returncode == 2:
            msg += (
                "\nHint: checkupdates exit 2 is a pacman error — install fakeroot "
                "(`sudo pacman -S fakeroot`), ensure no pacman lock, and run "
                "`checkupdates` in a shell for the full message"
            )
        raise RuntimeError(msg)
    return result.stdout.strip()


def truncate(text: str, limit: int = MAX_MSG_LEN) -> str:
    if len(text) <= limit:
        return text
    return "…\n" + text[-(limit - 2) :]


def send_update_notice(token: str, chat_id: str, body: str) -> None:
    resp = requests.post(
        TG_API.format(token=token, method="sendMessage"),
        json={
            "chat_id": chat_id,
            "text": body,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "Update now", "callback_data": CALLBACK_DATA}]
                ]
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def main() -> int:
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print("TG_BOT_TOKEN and TG_CHAT_ID must be set", file=sys.stderr)
        return 1

    repo_updates = run_cmd(["checkupdates"])
    aur_updates = run_cmd(["paru", "-Qua"])

    if not repo_updates and not aur_updates:
        return 0

    parts: list[str] = []
    if repo_updates:
        parts.append("Repo updates:\n" + repo_updates)
    if aur_updates:
        parts.append("AUR updates:\n" + aur_updates)

    message = truncate("\n\n".join(parts))
    send_update_notice(token, chat_id, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
