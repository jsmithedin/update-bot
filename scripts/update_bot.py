#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["requests"]
# ///
"""Long-polling Telegram listener for inline update callbacks.

IMPORTANT: Only one process may long-poll getUpdates per bot token. Before
deploying, confirm no other service (e.g. arr-stack notifications) uses
getUpdates on the same TG_BOT_TOKEN — a second consumer gets HTTP 409s.
"""

import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_LEN = 3500
CALLBACK_DATA = "sysupdate:run"
POLL_TIMEOUT = 30
UPDATE_TIMEOUT = 3600
PROGRESS_INTERVAL = 180
OFFSET_PATH = Path.home() / ".cache" / "update-bot-offset"
LOCK_PATH = Path.home() / ".cache" / "update-bot.lock"


def cmd_env() -> dict[str, str]:
    """Ensure system tools are on PATH for subprocesses (see update_checker.py)."""
    env = os.environ.copy()
    system = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env["PATH"] = f"{system}:{env.get('PATH', '')}"
    return env


def tg_request(token: str, method: str, **payload) -> dict:
    resp = requests.post(
        TG_API.format(token=token, method=method),
        json=payload,
        timeout=POLL_TIMEOUT + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error ({method}): {data}")
    return data


def send_message(token: str, chat_id: str, text: str) -> int:
    data = tg_request(token, "sendMessage", chat_id=chat_id, text=text)
    return data["result"]["message_id"]


def edit_message(token: str, chat_id: str, message_id: int, text: str) -> None:
    tg_request(
        token,
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
    )


def truncate(text: str, limit: int = MAX_MSG_LEN) -> str:
    if len(text) <= limit:
        return text
    return "…\n" + text[-(limit - 2) :]


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def load_offset() -> int:
    try:
        return int(OFFSET_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset))


def run_query_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=cmd_env())
    if result.returncode not in (0, 1):
        parts = [s for s in (result.stderr.strip(), result.stdout.strip()) if s]
        detail = "\n".join(parts) if parts else "(no output)"
        msg = f"{cmd[0]} failed (exit {result.returncode}): {detail}"
        if cmd[0] == "checkupdates" and result.returncode == 2:
            msg += (
                "\nHint: checkupdates exit 2 with no output often means pacman or "
                "fakeroot were not found on PATH inside the script (common under "
                "`uv run`)."
            )
        raise RuntimeError(msg)
    return result.stdout.strip()


def list_pending_updates() -> tuple[str, str]:
    return run_query_cmd(["checkupdates"]), run_query_cmd(["paru", "-Qua"])


def format_remaining(
    repo: str, aur: str, *, verification_failed: bool = False
) -> str:
    if verification_failed:
        return "Remaining updates: could not verify"
    if not repo and not aur:
        return "Remaining updates: none"
    parts: list[str] = []
    if repo:
        parts.append(f"Repo:\n{repo}")
    if aur:
        parts.append(f"AUR:\n{aur}")
    return "Still pending:\n\n" + "\n\n".join(parts)


def parse_failure_reason(output: str, exit_code: int) -> str | None:
    if exit_code == 0:
        return None

    lower = output.lower()
    if "unable to lock database" in lower:
        return "pacman database locked (another package manager may be running)"
    if "no space left on device" in lower:
        return "disk full"
    if re.search(
        r"incorrect password|authentication failure|a password is required",
        output,
        re.IGNORECASE,
    ):
        return "sudo blocked — check sudoers"
    if "failed to synchronize" in lower or "could not resolve host" in lower:
        return "network or mirror error"
    if "error: target not found" in lower:
        return "package not found in repos"

    match = re.search(r"error making:\s*(\S+)", output)
    if match:
        return f"AUR build failed for {match.group(1)}"
    if "error making:" in lower or "==> error:" in lower:
        return "AUR build failed"

    return None


def build_result_message(
    exit_code: int,
    output: str,
    repo: str,
    aur: str,
    *,
    timed_out: bool = False,
    verification_failed: bool = False,
) -> str:
    if timed_out:
        header = f"Update failed (timed out after {UPDATE_TIMEOUT}s)."
    elif exit_code == 0:
        header = "Update complete."
    else:
        reason = parse_failure_reason(output, exit_code)
        header = f"Update failed — {reason}" if reason else f"Update failed (exit {exit_code})."

    body = f"{header}\n\n{format_remaining(repo, aur, verification_failed=verification_failed)}"
    if output and (timed_out or exit_code != 0):
        remaining = MAX_MSG_LEN - len(body) - 2
        if remaining > 200:
            body += "\n\n" + truncate(output, remaining)
    return body


class UpdateLock:
    def __init__(self) -> None:
        self._fd: int | None = None

    def acquire(self) -> bool:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


def run_system_update(
    token: str,
    chat_id: str,
    progress_message_id: int,
) -> tuple[int, str]:
    start = time.monotonic()
    last_edit = start
    proc = subprocess.Popen(
        ["paru", "-Syu", "--noconfirm"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=cmd_env(),
    )

    try:
        while proc.poll() is None:
            elapsed = time.monotonic() - start
            if elapsed > UPDATE_TIMEOUT:
                proc.kill()
                proc.wait()
                stdout, stderr = proc.communicate()
                output = (stdout + stderr).strip()
                raise subprocess.TimeoutExpired(proc.args, UPDATE_TIMEOUT, output)

            now = time.monotonic()
            if now - last_edit >= PROGRESS_INTERVAL:
                edit_message(
                    token,
                    chat_id,
                    progress_message_id,
                    f"Update running… ({format_elapsed(elapsed)} elapsed)",
                )
                last_edit = now
            time.sleep(5)
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise

    stdout, stderr = proc.communicate()
    output = (stdout + stderr).strip()
    return proc.returncode, output


def handle_callback(token: str, chat_id: str, query: dict) -> None:
    query_id = query["id"]
    lock = UpdateLock()
    if not lock.acquire():
        tg_request(
            token,
            "answerCallbackQuery",
            callback_query_id=query_id,
            text="Update already in progress",
            show_alert=True,
        )
        return

    tg_request(token, "answerCallbackQuery", callback_query_id=query_id)

    try:
        progress_message_id = send_message(token, chat_id, "Starting update...")
        timed_out = False
        output = ""
        verification_failed = False

        try:
            exit_code, output = run_system_update(token, chat_id, progress_message_id)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            output = (exc.output or "").strip() if isinstance(exc.output, str) else ""

        try:
            repo, aur = list_pending_updates()
        except Exception as exc:
            print(f"Post-update check failed: {exc}", file=sys.stderr)
            repo, aur = "", ""
            verification_failed = True

        body = build_result_message(
            exit_code,
            output,
            repo,
            aur,
            timed_out=timed_out,
            verification_failed=verification_failed,
        )
        send_message(token, chat_id, body)
    finally:
        lock.release()


def process_update(token: str, chat_id: str, update: dict) -> None:
    query = update.get("callback_query")
    if not query:
        return

    if query.get("data") != CALLBACK_DATA:
        return

    message = query.get("message") or {}
    msg_chat = message.get("chat") or {}
    msg_chat_id = str(msg_chat.get("id", ""))
    if msg_chat_id != str(chat_id):
        return

    handle_callback(token, chat_id, query)


def poll_loop(token: str, chat_id: str) -> None:
    offset = load_offset()

    while True:
        data = tg_request(
            token,
            "getUpdates",
            offset=offset,
            timeout=POLL_TIMEOUT,
            allowed_updates=["callback_query"],
        )
        updates = data.get("result", [])

        for update in updates:
            update_id = update["update_id"]
            try:
                process_update(token, chat_id, update)
            except Exception as exc:
                print(f"Error handling update {update_id}: {exc}", file=sys.stderr)
            offset = update_id + 1
            save_offset(offset)


def main() -> int:
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print("TG_BOT_TOKEN and TG_CHAT_ID must be set", file=sys.stderr)
        return 1

    while True:
        try:
            poll_loop(token, chat_id)
        except requests.RequestException as exc:
            print(f"Poll error: {exc}", file=sys.stderr)
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
