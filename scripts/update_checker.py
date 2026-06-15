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
    """Environment for pacman/paru subprocesses.

    uv run may set TMPDIR to a cache directory that checkupdates/fakeroot
    cannot use for a temporary root, causing silent exit 2.
    """
    env = os.environ.copy()
    env["HOME"] = os.path.expanduser("~")
    system = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env["PATH"] = f"{system}:{env.get('PATH', '')}"
    env["TMPDIR"] = "/tmp"
    return env


def format_cmd_failure(cmd: list[str], result: subprocess.CompletedProcess[str], env: dict[str, str]) -> str:
    parts = [s for s in (result.stderr.strip(), result.stdout.strip()) if s]
    detail = "\n".join(parts) if parts else "(no output)"
    msg = f"{cmd[0]} failed (exit {result.returncode}): {detail}"
    if os.path.basename(cmd[-1]) == "checkupdates" or cmd[0] == "checkupdates":
        msg += (
            f"\nDebug: uv TMPDIR={os.environ.get('TMPDIR')!r}, "
            f"subprocess TMPDIR={env.get('TMPDIR')!r}"
        )
        trace = subprocess.run(
            ["/usr/bin/bash", "-x", CHECKUPDATES],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        trace_lines = (trace.stderr + trace.stdout).splitlines()
        if trace_lines:
            msg += "\nTrace tail:\n" + "\n".join(trace_lines[-8:])
    return msg


def run_checkupdates() -> str:
    env = cmd_env()
    attempts: list[list[str]] = [
        [CHECKUPDATES],
        ["/usr/bin/bash", "-lc", CHECKUPDATES],
    ]
    last: subprocess.CompletedProcess[str] | None = None
    for cmd in attempts:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        last = result
        if result.returncode in (0, 1):
            return result.stdout.strip()
    assert last is not None
    raise RuntimeError(format_cmd_failure(attempts[-1], last, env))


def run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=cmd_env())
    if result.returncode not in (0, 1):
        raise RuntimeError(format_cmd_failure(cmd, result, cmd_env()))
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
