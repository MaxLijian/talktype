#!/usr/bin/env python3
"""
TalkType Text Receiver - Run this on the B machine to receive transcribed text.

Connects to the relay server via WebSocket and pastes received text into
whatever input field currently has focus on this machine.

Works on macOS, Windows, and Linux. Handles Chinese and all Unicode text.

Usage:
    python text_receiver.py --relay https://your-relay.onrender.com --room YOUR_SECRET_ROOM_ID

    # Or with environment variables:
    TALKTYPE_RELAY=https://your-relay.onrender.com TALKTYPE_ROOM=my-secret python text_receiver.py

    # Auto-reconnects on disconnect. Keep it running in the background.

Requirements (pip install):
    websockets pyperclip

    macOS:   nothing extra (uses pbcopy + osascript)
    Windows: pyautogui
    Linux:   xdotool + xclip  (sudo apt install xdotool xclip)
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import time
import threading

SYSTEM = platform.system()  # "Darwin", "Windows", "Linux"

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("receiver")


# === Paste Logic ===

def _check_macos_accessibility() -> bool:
    """Check if terminal has macOS Accessibility permission for keystroke injection."""
    result = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of first process whose frontmost is true'],
        capture_output=True, timeout=3,
    )
    return result.returncode == 0


def _paste_macos(text: str):
    """
    macOS: copy to clipboard via pbcopy, then send Cmd+V via osascript.
    Requires Accessibility permission for the terminal running this script.
    """
    # Save old clipboard
    try:
        old_clip = subprocess.check_output(["pbpaste"], stderr=subprocess.DEVNULL)
    except Exception:
        old_clip = None

    # Write new text to clipboard
    proc = subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        logger.error("pbcopy failed")
        return

    time.sleep(0.05)

    # Paste into focused window via Cmd+V
    result = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using {command down}'],
        capture_output=True,
    )
    if result.returncode != 0:
        logger.error("osascript failed — likely missing Accessibility permission")
        logger.error("Fix: System Settings → Privacy & Security → Accessibility → add your terminal app")

    # Restore old clipboard after a short delay
    if old_clip is not None:
        def restore():
            time.sleep(0.8)
            subprocess.run(["pbcopy"], input=old_clip, stderr=subprocess.DEVNULL)
        threading.Thread(target=restore, daemon=True).start()


def _paste_windows(text: str):
    """
    Windows: clipboard + Ctrl+V.

    Why not SendInput with KEYEVENTF_UNICODE?
    - KEYEVENTF_UNICODE injects low-level key events but still goes through IME
    - IME can intercept and re-process Chinese characters -> garbled output
    - Clipboard paste (WM_PASTE / Ctrl+V) delivers raw Unicode text directly
    - Works in all apps: browsers, chat apps, IDEs, native dialogs
    """
    try:
        import pyperclip
        import pyautogui
    except ImportError:
        logger.error("Missing dependencies: pip install pyperclip pyautogui")
        return

    try:
        old_clip = pyperclip.paste()
    except Exception:
        old_clip = None

    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")

    if old_clip is not None:
        def restore():
            time.sleep(0.8)
            try:
                pyperclip.copy(old_clip)
            except Exception:
                pass
        threading.Thread(target=restore, daemon=True).start()


def _paste_linux(text: str):
    """
    Linux: try xdotool type first (handles Unicode natively, no clipboard),
    fall back to xclip + xdotool key ctrl+v if type fails.

    xdotool type --clearmodifiers handles Chinese on X11 because it uses
    XSendEvent with proper keysym lookup — no clipboard needed.
    """
    # Try direct typing first (preferred: no clipboard pollution)
    result = subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "20", "--", text],
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return

    # Fallback: clipboard + paste
    logger.debug("xdotool type failed, falling back to clipboard paste")
    try:
        # Save old clipboard
        old_clip = subprocess.check_output(
            ["xclip", "-selection", "clipboard", "-o"],
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        old_clip = None

    # Set new clipboard
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=text.encode("utf-8"),
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.05)

    # Paste
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
        stderr=subprocess.DEVNULL,
    )

    # Restore
    if old_clip is not None:
        def restore():
            time.sleep(0.8)
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=old_clip,
                stderr=subprocess.DEVNULL,
            )
        threading.Thread(target=restore, daemon=True).start()


def paste_text(text: str):
    """Paste text into the currently focused window, handling Chinese correctly."""
    if not text:
        return

    logger.info(f"Pasting: {text[:60]}{'...' if len(text) > 60 else ''}")

    try:
        if SYSTEM == "Darwin":
            _paste_macos(text)
        elif SYSTEM == "Windows":
            _paste_windows(text)
        elif SYSTEM == "Linux":
            _paste_linux(text)
        else:
            logger.error(f"Unsupported OS: {SYSTEM}")
    except Exception as e:
        logger.error(f"Paste failed: {e}")


# === WebSocket Client ===

async def receive_loop(relay_url: str, room_id: str):
    """
    Connect to relay and paste received text. Auto-reconnects on disconnect.
    """
    try:
        import websockets
    except ImportError:
        logger.error("Missing dependency: pip install websockets")
        sys.exit(1)

    ws_url = relay_url.rstrip("/")
    ws_url = ws_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/ws/{room_id}"

    logger.info(f"Connecting to relay: {ws_url}")
    logger.info(f"Room: {room_id[:8]}...")
    logger.info(f"OS: {SYSTEM} — ready to paste into any focused window")

    retry_delay = 2  # seconds, doubles up to 30

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                retry_delay = 2  # reset on successful connect
                logger.info("Connected! Waiting for transcriptions...")

                async for raw_message in ws:
                    try:
                        msg = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue

                    # Handle keepalive ping from relay
                    if msg.get("ping"):
                        await ws.send("ping")
                        continue

                    # Handle connected acknowledgment
                    if msg.get("status") == "connected":
                        logger.info(f"Relay confirmed: room {msg.get('room')}")
                        continue

                    text = msg.get("text", "").strip()
                    if text:
                        # Run paste in a thread so it doesn't block the event loop
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, paste_text, text)

        except Exception as e:
            err_msg = str(e)
            if "ConnectionRefused" in err_msg or "Cannot connect" in err_msg:
                logger.error(f"Cannot reach relay server at {ws_url}")
                logger.error("Check that the relay server is running and the URL is correct")
            else:
                logger.warning(f"Disconnected: {e}")

            logger.info(f"Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)


# === Dependency Check ===

def check_dependencies():
    """Verify system dependencies are available."""
    if SYSTEM == "Linux":
        missing = []
        for cmd in ("xdotool", "xclip"):
            result = subprocess.run(["which", cmd], capture_output=True)
            if result.returncode != 0:
                missing.append(cmd)
        if missing:
            logger.error(f"Missing Linux dependencies: {', '.join(missing)}")
            logger.error(f"Install with: sudo apt install {' '.join(missing)}")
            sys.exit(1)

    elif SYSTEM == "Windows":
        try:
            import pyautogui
        except ImportError:
            logger.error("Missing dependency: pip install pyautogui")
            sys.exit(1)

    elif SYSTEM == "Darwin":
        if not _check_macos_accessibility():
            logger.error("=" * 55)
            logger.error("macOS Accessibility permission required but not granted.")
            logger.error("Steps to fix:")
            logger.error("  1. Open: System Settings → Privacy & Security → Accessibility")
            logger.error("  2. Click '+' and add your terminal app (Terminal / iTerm2)")
            logger.error("  3. Enable the toggle next to it")
            logger.error("  4. Re-run this script")
            logger.error("=" * 55)
            sys.exit(1)

    try:
        import websockets  # noqa
    except ImportError:
        logger.error("Missing dependency: pip install websockets")
        sys.exit(1)


# === Main ===

def parse_args():
    relay_default = os.environ.get("TALKTYPE_RELAY", "")
    room_default = os.environ.get("TALKTYPE_ROOM", "")

    parser = argparse.ArgumentParser(
        description="TalkType receiver - paste transcribed speech into any window",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python text_receiver.py --relay https://myrelay.onrender.com --room mysecretroom
  TALKTYPE_RELAY=https://myrelay.onrender.com TALKTYPE_ROOM=mysecret python text_receiver.py

Generate a room ID:
  python -c "import uuid; print(uuid.uuid4())"
        """,
    )
    parser.add_argument(
        "--relay", "-r",
        default=relay_default,
        required=not relay_default,
        help="Relay server URL (e.g. https://myrelay.onrender.com)",
    )
    parser.add_argument(
        "--room", "-R",
        default=room_default,
        required=not room_default,
        help="Shared room ID / secret (must match talktype.py --room)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    logger.setLevel(getattr(logging, args.log_level))

    check_dependencies()

    print("TalkType Receiver")
    print("=" * 45)
    print(f"Relay: {args.relay}")
    print(f"Room:  {args.room[:8]}...")
    print(f"OS:    {SYSTEM}")
    print("Make sure your cursor is in the target input field before speaking.")
    print("Press Ctrl+C to exit.\n")

    try:
        asyncio.run(receive_loop(args.relay, args.room))
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
