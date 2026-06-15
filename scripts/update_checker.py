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
CHECKUPDATES = "/usr/bin/checkupdates"


def cmd_env() -> dict[str, str]:
    """Environment for pacman/paru subprocesses."""
    env = os.environ.copy()
    env["HOME"] = os.path.expanduser("~")
    system = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env["PATH"] = f"{system}:{env.get('PATH', '')}"
    env["TMPDIR"] = "/tmp"
    return env


def parse_checkupdates(result: subprocess.CompletedProcess[str]) -> str:
    """Return pending repo updates, or "" if none.

    pacman-contrib changed exit codes in 1.13.1 (Dec 2025):
      old: 0 = none, 1 = updates, 2 = error
      new: 0 = updates, 1 = error, 2 = none
    """
    code = result.returncode
    out = result.stdout.strip()
    err = result.stderr.strip()

    if code == 0:
        return out
    if code == 1:
        if out:
            return out  # old: updates available
        detail = err or "(no output)"
        raise RuntimeError(f"checkupdates failed (exit 1): {detail}")
    if code == 2:
        if err:
            detail = f"{err}\n{out}".strip() if out else err
            raise RuntimeError(f"checkupdates failed (exit 2): {detail}")
        return out  # new: no updates
    detail = err or out or "(no output)"
    raise RuntimeError(f"checkupdates failed (exit {code}): {detail}")


def run_checkupdates() -> str:
    result = subprocess.run(
        [CHECKUPDATES],
        capture_output=True,
        text=True,
        env=cmd_env(),
    )
    return parse_checkupdates(result)


def run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=cmd_env())
    if result.returncode not in (0, 1):
        parts = [s for s in (result.stderr.strip(), result.stdout.strip()) if s]
        detail = "\n".join(parts) if parts else "(no output)"
        raise RuntimeError(f"{cmd[0]} failed (exit {result.returncode}): {detail}")
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

    repo_updates = run_checkupdates()
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
