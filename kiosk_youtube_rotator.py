#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kiosk_youtube_rotator.py
---
Rotate through YouTube URLs in Chrome on Windows 10/11.

Features:
- Opens first URL, maximizes the window, and toggles the YouTube player to fullscreen (not OS-level fullscreen).
- Preloads the next URL in a minimized/background window, waits for it to load,
  optionally waits an extra --handoff-delay, then closes the old window and brings the new one forward.
- Can use your default Chrome profile (stay logged in) or a separate private profile.
- Uses Chrome DevTools Protocol (CDP) for reliable per-window control (no fragile SendKeys).
- Periodically downloads its playlists from remote URLs (e.g. raw GitHub files)
  in the background, so the lists can be edited remotely without restarting and
  without depending on git.

Two kinds of input file
-----------------------
--config  : a ".conf" file listing REMOTE URL-list files to download, one URL
            per line (blanks and "#" comments ignored). Each downloaded file is
            a playlist in the "URL-list format" described below. These are
            re-downloaded periodically (see --lists-refresh-interval) into a
            local cache dir (default ./downloaded_lists) that survives restarts.
            If a download fails but a cached copy exists, the cached copy is
            used; if there is no cached copy and nothing else to play, the
            program exits with an error.
--urls    : an OPTIONAL local playlist file in the same URL-list format. Its
            links are merged with the links from the downloaded lists.

URL-list format
---------------
Each non-empty / non-comment line in a playlist file describes how to play a video.

Examples:

    # this is a comment - ignored
    link1
    link2&t=%s:200
    300:link3&start=%s:200
    200:link4

Semantics:

1)  "link1"
    A plain link. The video is played, and after a RANDOM delay in the range
    [--min-delay, --max-delay] we switch to another video.

2)  "link2&t=%s:200"
    If the link contains "%s" and the line ends with ":N", then "%s" is
    replaced with a random number of seconds from 0..N (inclusive). This
    randomizes the start position of the video.

3)  "300:link3&start=%s:200"
    A leading "<number>:" prefix, e.g. "300:", denotes an extra playback time
    (extra_delay) in seconds that is added on top of the standard random delay
    from [--min-delay, --max-delay]. In this example: base random delay + 300s.
    The trailing "%s:200" behaves as in case 2 - a random start in 0..200.

4)  "200:link4"
    Only the "200:" prefix here, so link4 is played at least 200 seconds longer
    than the standard random delay would dictate. No random start ("%s" absent).

5)  The URL pool is re-read after EVERY video switch, so links can be added or
    removed (locally, or remotely on the next refresh) without restarting.

Dependencies
------------
    pip install requests websocket-client

URL-list files are downloaded with `requests`, so no external command-line
downloader (wget/curl) needs to be present on the kiosk box.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from websocket import WebSocket, create_connection

DEFAULT_MIN_DELAY: int = 120  # seconds (2 minutes)
DEFAULT_MAX_DELAY: int = 600  # seconds (10 minutes)

# How often the background thread re-downloads the URL-list files referenced by
# the --config file. Overridable with --lists-refresh-interval.
DEFAULT_LISTS_REFRESH_INTERVAL: int = 300  # seconds (5 minutes)

# Downloaded URL-list files are cached here, in a fixed directory next to the
# current working directory, so they survive restarts. If a download fails we
# fall back to whatever copy is already present here.
DEFAULT_LISTS_DIR_NAME: str = "downloaded_lists"

# How long (seconds) a single URL-list download is allowed to take before it is
# treated as a failure. requests applies this to both connect and read phases,
# so a hung server cannot stall the refresher thread or initial startup.
LISTS_FETCH_TIMEOUT: float = 60.0

# Sub-directory name (under the system temp dir) where the HTTP conditional-request
# validators (ETag / Last-Modified) for each downloaded list are cached. These let
# us ask the server "only send the body if it changed" (If-None-Match /
# If-Modified-Since) instead of re-downloading every time. They live in the temp
# dir on purpose: losing them only costs one extra full download, so a
# system-dependent, possibly-volatile location is perfectly acceptable.
DEFAULT_VALIDATORS_DIR_NAME: str = "kiosk_youtube_rotator_meta"


def default_validators_dir() -> Path:
    """Return the default directory for cached conditional-request validators.

    Resolved from the system temp dir (``tempfile.gettempdir()``), which honors
    ``TMPDIR``/``TEMP``/``TMP`` so the location stays flexible per platform and
    per environment.
    """
    return Path(tempfile.gettempdir()) / DEFAULT_VALIDATORS_DIR_NAME


# -------------------------
# Utilities & Chrome launch
# -------------------------


def find_chrome_exe(user_supplied: Optional[str] = None) -> str:
    """Find chrome.exe path.

    Prefer user-supplied value, otherwise try common locations and PATH.
    """
    if user_supplied:
        try:
            p = os.path.expandvars(os.path.expanduser(user_supplied))
            if os.path.exists(p):
                return p
        except Exception as exc:  # pragma: no cover - extremely unlikely
            print(f"[WARN] Failed to resolve user supplied Chrome path: {exc}", file=sys.stderr)

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        try:
            if os.path.exists(c):
                return c
        except Exception:
            # os.path.exists should not fail, but do not crash if it does
            continue

    try:
        found = shutil.which("chrome") or shutil.which("chrome.exe")
    except Exception as exc:  # pragma: no cover - defensive
        raise FileNotFoundError(f"Failed to search Chrome in PATH: {exc}") from exc

    if found:
        return found

    raise FileNotFoundError("Chrome executable not found. Specify with --chrome.")


def is_chrome_running() -> bool:
    """Check if any chrome.exe processes are running (Windows-only heuristic)."""
    if os.name != "nt":
        # On non-Windows we don't know -> assume not running.
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return "chrome.exe" in out.lower()
    except Exception:
        return False


def start_chrome(
    chrome_exe: str,
    remote_port: int,
    user_data_dir: Path,
    profile_directory: Optional[str] = None,
) -> subprocess.Popen[Any]:
    """Start Chrome with remote debugging and return the Popen object.

    Chrome is bound to localhost only and remote-allow-origins is relaxed so
    that our DevTools websocket Origin is accepted (avoids a Chrome 403).
    """
    args: List[str] = [
        chrome_exe,
        "--kiosk",
        f"--remote-debugging-port={remote_port}",
        "--remote-debugging-address=127.0.0.1",  # bind to localhost only
        "--remote-allow-origins=*",  # accept our DevTools websocket Origin
        "--disable-restore-session-state",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        f"--user-data-dir={user_data_dir}",
        "--new-window",
        "https://youtube.com",
    ]
    # Select a specific profile directory inside the user-data-dir when asked
    # (e.g. "Default", "Profile 1"). Left unset for a fresh private profile.
    if profile_directory:
        args.append(f"--profile-directory={profile_directory}")

    creationflags: int = 0
    if os.name == "nt":
        # Start in a new process group, detached from this console.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]

    try:
        proc = subprocess.Popen(
            args,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to start Chrome: {exc}") from exc

    return proc


def wait_for_debug_port(remote_port: int, timeout: float = 20.0) -> str:
    """Wait for http://127.0.0.1:<port>/json/version and return browser websocket URL."""
    start = time.time()
    url = f"http://127.0.0.1:{remote_port}/json/version"
    last_err: Optional[Exception] = None

    while time.time() - start < timeout:
        try:
            resp = requests.get(url, timeout=1.5)
            if resp.ok:
                j = resp.json()
                ws_url = j.get("webSocketDebuggerUrl")
                if isinstance(ws_url, str) and ws_url:
                    return ws_url
        except Exception as exc:
            last_err = exc
        time.sleep(0.25)

    raise RuntimeError(f"Chrome remote debugging port {remote_port} not responding: {last_err}")


# -------------------------
# DevTools connection
# -------------------------


class CDPConnection:
    """Thin wrapper for a single DevTools websocket connection."""

    def __init__(self, ws_url: str) -> None:
        from urllib.parse import urlparse

        u = urlparse(ws_url)  # ws://127.0.0.1:9222/devtools/browser/...
        host = u.hostname or "127.0.0.1"
        port = u.port or 9222
        origin = f"http://{host}:{port}"  # Must match --remote-allow-origins

        try:
            # Set Origin to avoid 403
            self.ws: WebSocket = create_connection(ws_url, timeout=10, origin=origin)
        except Exception as exc:
            raise RuntimeError(f"Failed to open DevTools websocket: {exc}") from exc

        self._id: int = 0
        self._event_buffer: List[Dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        mid = self._next_id()
        payload: Dict[str, Any] = {"id": mid, "method": method}
        if params:
            payload["params"] = params
        try:
            self.ws.send(json.dumps(payload))
        except Exception as exc:
            raise RuntimeError(f"Failed to send CDP message {method}: {exc}") from exc
        return mid

    def recv(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if timeout is not None:
            try:
                self.ws.settimeout(timeout)
            except Exception:
                # best effort; ignore
                pass
        try:
            raw = self.ws.recv()
        except Exception:
            return None
        try:
            msg: Dict[str, Any] = json.loads(raw)
            return msg
        except Exception:
            return None

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Send a command and wait for the matching response."""
        mid = self.send(method, params)
        end = time.time() + timeout

        while time.time() < end:
            msg = self.recv(timeout=max(0.0, end - time.time()))
            if not msg:
                continue

            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error for {method}: {msg['error']}")
                result = msg.get("result")
                if isinstance(result, dict):
                    return result
                return {}
            if "method" in msg:
                self._event_buffer.append(msg)

        raise TimeoutError(f"Timeout waiting for response to {method}")

    def wait_event(
        self,
        method: str,
        timeout: float = 30.0,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        """Wait for a specific event name, optionally filtered by predicate."""
        end = time.time() + timeout

        # First, check buffered events
        i = 0
        while i < len(self._event_buffer):
            ev = self._event_buffer[i]
            if ev.get("method") == method and (predicate is None or predicate(ev)):
                del self._event_buffer[i]
                return ev
            i += 1

        # Then read new events
        while time.time() < end:
            msg = self.recv(timeout=max(0.0, end - time.time()))
            if not msg:
                continue
            if msg.get("method") == method and (predicate is None or predicate(msg)):
                return msg
            self._event_buffer.append(msg)

        raise TimeoutError(f"Timeout waiting for event {method}")


@dataclass
class ManagedWindow:
    target_id: str
    window_id: int
    page_ws_url: str
    page_conn: CDPConnection


# -------------------------
# Chrome controller
# -------------------------


class ChromeController:
    def __init__(self, remote_port: int) -> None:
        self.remote_port: int = remote_port
        ws_url = wait_for_debug_port(remote_port)
        self.browser_ws_url: str = ws_url
        self.browser_conn: CDPConnection = CDPConnection(ws_url)

    def list_targets(self) -> List[Dict[str, Any]]:
        url = f"http://127.0.0.1:{self.remote_port}/json"
        try:
            resp = requests.get(url, timeout=2)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data  # type: ignore[return-value]
        except Exception as exc:
            raise RuntimeError(f"Failed to list Chrome targets: {exc}") from exc
        return []

    def get_page_ws_url(self, target_id: str) -> str:
        """Get websocket debugger URL for a specific target."""
        for endpoint in ("/json/list", "/json/targets"):
            url = f"http://127.0.0.1:{self.remote_port}{endpoint}"
            try:
                resp = requests.get(url, timeout=2)
                if not resp.ok:
                    continue
                for t in resp.json():
                    if t.get("id") == target_id:
                        ws_url = t.get("webSocketDebuggerUrl")
                        if isinstance(ws_url, str) and ws_url:
                            return ws_url
            except Exception:
                continue
        raise RuntimeError(f"Could not find ws URL for target {target_id}")

    def create_new_window_target(self) -> str:
        res = self.browser_conn.call(
            "Target.createTarget",
            {"url": "https://youtube.com", "newWindow": True},
        )
        target_id = res.get("targetId")
        if not isinstance(target_id, str):
            raise RuntimeError("Target.createTarget did not return a targetId")
        return target_id

    def get_window_id_for_target(self, target_id: str) -> int:
        res = self.browser_conn.call("Browser.getWindowForTarget", {"targetId": target_id})
        window_id = res.get("windowId")
        if not isinstance(window_id, int):
            raise RuntimeError("Browser.getWindowForTarget did not return an int windowId")
        return window_id

    def set_window_state(self, window_id: int, state: str) -> None:
        """Set window state: 'normal', 'minimized', 'maximized', 'fullscreen'."""
        try:
            self.browser_conn.call(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": state}},
            )
        except Exception as exc:
            print(f"[WARN] Failed to set window state to {state}: {exc}", file=sys.stderr)

    def activate_target(self, target_id: str) -> None:
        try:
            self.browser_conn.call("Target.activateTarget", {"targetId": target_id})
        except Exception as exc:
            print(f"[WARN] Failed to activate target {target_id}: {exc}", file=sys.stderr)

    def close_target(self, target_id: str) -> None:
        try:
            self.browser_conn.call("Target.closeTarget", {"targetId": target_id})
        except Exception as exc:
            print(f"[WARN] Failed to close target {target_id}: {exc}", file=sys.stderr)

    def connect_page(self, target_id: str) -> ManagedWindow:
        page_ws = self.get_page_ws_url(target_id)
        page_conn = CDPConnection(page_ws)

        # Enable useful domains (best effort)
        for method, params in (
            ("Page.enable", None),
            ("Network.enable", None),
            ("Page.setLifecycleEventsEnabled", {"enabled": True}),
        ):
            try:
                if params:
                    page_conn.call(method, params)
                else:
                    page_conn.call(method)
            except Exception:
                # Non-fatal; continue
                pass

        window_id = self.get_window_id_for_target(target_id)
        return ManagedWindow(
            target_id=target_id,
            window_id=window_id,
            page_ws_url=page_ws,
            page_conn=page_conn,
        )

    def navigate_and_wait(self, mw: ManagedWindow, url: str, timeout: float = 90.0) -> None:
        """Navigate to url and wait until page seems reasonably loaded."""
        try:
            mw.page_conn.call("Page.navigate", {"url": url})
        except Exception as exc:
            raise RuntimeError(f"Failed to navigate to {url}: {exc}") from exc

        # Wait for lifecycle 'networkIdle' or 'DOMContentLoaded'; fallback to loadEventFired
        try:
            mw.page_conn.wait_event(
                "Page.lifecycleEvent",
                timeout=timeout,
                predicate=lambda e: e.get("params", {}).get("name") in ("networkIdle", "DOMContentLoaded"),
            )
        except TimeoutError:
            try:
                mw.page_conn.wait_event("Page.loadEventFired", timeout=timeout)
            except Exception:
                # If that also times out, continue anyway – page might still be usable.
                pass

    def try_autoplay_and_fullscreen_player(
        self,
        mw: ManagedWindow,
        player_fullscreen: bool = False,
        mute: bool = True,
    ) -> None:
        """
        Best-effort to:
        1) Mute/unmute and start playback.
        2) Optionally toggle YouTube player's fullscreen (not OS fullscreen).
        """

        # 1) Mute & play for autoplay reliability
        js_play = """
        (function(){
            const v = document.querySelector('video');
            if (v) {
                try { v.muted = %s; } catch(e){}
                try { v.play().catch(()=>{}); } catch(e){}
            }
            return !!v;
        })();
        """ % ("true" if mute else "false")

        try:
            mw.page_conn.call(
                "Runtime.evaluate",
                {"expression": js_play, "awaitPromise": False, "returnByValue": True},
            )
        except Exception:
            # non-fatal
            pass

        if not player_fullscreen:
            return

        # 2) Bring to front so input counts as a user gesture
        try:
            self.activate_target(mw.target_id)
        except Exception:
            pass

        # 3) Try to focus the video element (click at its center)
        try:
            res = mw.page_conn.call(
                "Runtime.evaluate",
                {
                    "expression": """
                    (function(){
                        const v = document.querySelector('video');
                        if (!v) return null;
                        const r = v.getBoundingClientRect();
                        return {
                            x: Math.round(r.left + r.width / 2),
                            y: Math.round(r.top + r.height / 2)
                        };
                    })();
                    """,
                    "returnByValue": True,
                },
                timeout=3.0,
            )
            coords = (res or {}).get("result", {}).get("value")
            if isinstance(coords, dict) and "x" in coords and "y" in coords:
                x = int(coords["x"])
                y = int(coords["y"])
            else:
                x, y = 400, 300  # fallback click position

            for event_type in ("mousePressed", "mouseReleased"):
                try:
                    mw.page_conn.call(
                        "Input.dispatchMouseEvent",
                        {
                            "type": event_type,
                            "x": x,
                            "y": y,
                            "button": "left",
                            "clickCount": 1,
                        },
                    )
                except Exception:
                    pass
        except Exception:
            # any JS / CDP failure is non-fatal
            pass

        # Small wait to allow focus to settle
        time.sleep(3.5)

        # 4) Toggle player fullscreen via 'f'
        try:
            for t in ("rawKeyDown", "keyUp"):
                mw.page_conn.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": t,
                        "key": "f",
                        "code": "KeyF",
                        "windowsVirtualKeyCode": 0x46,
                        "nativeVirtualKeyCode": 0x46,
                    },
                )
        except Exception:
            pass

        # 5) Button click fallback (in case 'f' didn't work)
        try:
            mw.page_conn.call(
                "Runtime.evaluate",
                {
                    "expression": "document.querySelector('.ytp-fullscreen-button, button.ytp-fullscreen-button')?.click();",
                    "awaitPromise": False,
                    "returnByValue": False,
                },
            )
        except Exception:
            pass


# -------------------------
# Helpers for URLs & config
# -------------------------


@dataclass
class URLConfig:
    """
    Parsed configuration for a single URL line.

    template:
        Base URL (may contain a "%s" placeholder for start time).
    extra_delay:
        Additional seconds added ON TOP of the random delay from
        --min-delay/--max-delay.
    max_start_offset:
        If not None, "%s" in template is replaced by a random integer from
        0..max_start_offset (inclusive) when the URL is used.
    """

    template: str
    extra_delay: int = 0
    max_start_offset: Optional[int] = None


def parse_config_line(line: str) -> Optional[URLConfig]:
    """
    Parse a single line of the config file.

    Supported forms:
    - "link1"
    - "link2&t=%s:200"
    - "300:link3&start=%s:200"
    - "200:link4"

    Rules:
    - A leading "<number>:" at the very start of the line -> extra_delay
      (additional seconds added on top of the random delay).
    - A trailing ":<number>" only if the URL contains "%s" -> max_start_offset.
      This defines the range [0, max_start_offset] for the random start time.
    - Empty lines and lines starting with "#" are ignored.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    extra_delay: int = 0
    max_start_offset: Optional[int] = None
    template: str = s

    # 1) Leading "<seconds>:" prefix, only at the very beginning of the line.
    m = re.match(r"^(\d+):(.*)$", template)
    if m:
        extra_delay = int(m.group(1))
        template = m.group(2)

    # 2) Trailing ":<seconds>" suffix, only when "%s" appears in the URL.
    m = re.match(r"^(.*%s.*):(\d+)$", template)
    if m:
        template = m.group(1)
        max_start_offset = int(m.group(2))

    return URLConfig(template=template, extra_delay=extra_delay, max_start_offset=max_start_offset)


def load_urls(path: Path) -> List[URLConfig]:
    """
    Load the entire config file as a list of URLConfig entries.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    urls: List[URLConfig] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                entry = parse_config_line(raw_line)
                if entry is not None:
                    urls.append(entry)
    except Exception as exc:
        raise RuntimeError(f"Failed to read config file '{path}': {exc}") from exc

    if not urls:
        raise ValueError(f"Config file '{path}' has no URLs.")
    return urls


def choose_url(urls: List[URLConfig], exclude_template: Optional[str] = None) -> URLConfig:
    """
    Randomly pick a URLConfig, optionally excluding the same template as the
    previous pick (so the same video does not play twice in a row).

    If the only available entry is the excluded one, it is returned anyway
    rather than failing.
    """
    if not urls:
        raise ValueError("No URLs available to choose from.")
    choices = [u for u in urls if u.template != exclude_template] or urls
    return random.choice(choices)


def render_url(cfg: URLConfig) -> str:
    """
    Produce a concrete URL for the given URLConfig:

    - if max_start_offset is set and the template contains "%s", pick a random
      second from [0, max_start_offset] and substitute it,
    - otherwise return the template unchanged.
    """
    url = cfg.template
    if cfg.max_start_offset is not None and "%s" in url:
        start_sec = random.randint(0, cfg.max_start_offset)
        try:
            url = url % start_sec
        except TypeError:
            # If the URL contains other stray "%" characters, give up on
            # substitution and use the template as-is.
            pass
    return url


def ensure_query_param(url: str, key: str, value: str) -> str:
    """Ensure that the URL has the given query parameter (do not override if present)."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    pr = urlparse(url)
    try:
        q = dict(parse_qsl(pr.query))
    except Exception:
        q = {}
    if key not in q:
        q[key] = value
    new_q = urlencode(q)
    return urlunparse(pr._replace(query=new_q))


# -------------------------
# Remote URL-list downloading
# -------------------------
#
# The --config file lists *remote URL-list files* (one URL per line, blank lines
# and "#" comments ignored), e.g. raw.githubusercontent.com/.../jazz.txt. A
# background thread periodically downloads each of them into DEFAULT_LISTS_DIR
# and the rotation loop reads the cached copies. This lets the playlists be
# edited on GitHub (or anywhere reachable over HTTP) without restarting, and
# without depending on git being installed.


def parse_lists_config(path: Path) -> List[str]:
    """Read the --config file and return the list of remote URL-list URLs.

    One URL per line; blank lines and lines starting with "#" are ignored.
    Raises FileNotFoundError if the file is missing and ValueError if it
    contains no usable URLs.
    """
    if not path.exists():
        raise FileNotFoundError(f"Lists config file not found: {path}")

    out: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            s = raw_line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)

    if not out:
        raise ValueError(f"Lists config file '{path}' has no URLs.")
    return out


def list_filename_for_url(url: str) -> str:
    """Build a stable, filesystem-safe local filename for a remote list URL.

    The basename of the URL is kept for human readability and combined with a
    short hash of the full URL, so two different URLs that happen to share a
    basename (e.g. two different ".../list.txt") never collide.
    """
    from urllib.parse import urlparse

    path_part = urlparse(url).path
    base = os.path.basename(path_part) or "list"
    # Keep only conservative characters; replace anything else with "_".
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not base.endswith(".txt"):
        base += ".txt"

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{digest}_{base}"


def fetch_list(url: str, dest_dir: Path) -> bool:
    """Download a single remote URL-list file into dest_dir.

    Uses the `requests` library directly (already a dependency for the CDP
    layer) so the kiosk box needs no external downloader on PATH. The download
    follows redirects, treats any non-2xx HTTP status as a failure, and is
    written to a ".part" temp file that is atomically promoted only on success,
    so a failed/partial download never clobbers a previously good cached copy.

    Returns True if a fresh copy was downloaded, False if the download failed
    but a previously cached copy already exists (in which case the old copy is
    kept and a warning is printed).

    Raises RuntimeError if the download fails AND no cached copy exists, since
    that list then has no usable content at all.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / list_filename_for_url(url)
    tmp = dest.with_name(dest.name + ".part")

    ok = False
    try:
        resp = requests.get(url, timeout=LISTS_FETCH_TIMEOUT, allow_redirects=True)
        # Any 4xx/5xx must be a failure so we keep the cache instead of caching
        # an error page; raise_for_status() turns those into an exception.
        resp.raise_for_status()
        content = resp.content
        # An empty body is treated as a failed download (mirrors the previous
        # behaviour) so an empty response never replaces a good cached list.
        if content:
            tmp.write_bytes(content)
            ok = tmp.exists() and tmp.stat().st_size > 0
    except Exception as exc:  # network error, timeout, HTTP error, etc.
        ok = False
        print(f"[WARN] Download error for {url}: {exc}", file=sys.stderr)

    if ok:
        # Atomically replace the cached copy with the freshly downloaded one.
        os.replace(tmp, dest)
        return True

    # Clean up any partial file.
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    if dest.exists():
        print(
            f"[WARN] Could not download {url}; using cached copy at {dest}.",
            file=sys.stderr,
        )
        return False

    raise RuntimeError(f"Failed to download {url} and no cached copy exists.")


def refresh_lists(list_urls: List[str], dest_dir: Path) -> int:
    """Download all remote URL-list files. Returns how many were freshly fetched.

    A failure on any single list is fatal only when that list has no cached
    copy (fetch_list raises); otherwise the cached copy is kept.
    """
    fetched = 0
    for url in list_urls:
        if fetch_list(url, dest_dir):
            fetched += 1
    return fetched


# -------------------------
# Conditional (ETag / Last-Modified) downloading
# -------------------------
#
# fetch_list() above always downloads the full body. The functions below add an
# opt-in, "smarter" path used by the background refresher: they remember the
# server's validators (ETag / Last-Modified) for each list and replay them as
# If-None-Match / If-Modified-Since on the next request. The server then answers
# "304 Not Modified" (empty body) when nothing changed, so we only pull — and
# only log "remote newer" — when the remote copy is genuinely newer. The
# original fetch_list()/refresh_lists() are left untouched.


def validators_path_for_url(meta_dir: Path, url: str) -> Path:
    """Path of the JSON sidecar holding the conditional-request validators.

    Reuses ``list_filename_for_url`` so the sidecar is keyed by the same stable,
    collision-free name as the cached list itself.
    """
    return meta_dir / (list_filename_for_url(url) + ".meta.json")


def read_validators(meta_dir: Path, url: str) -> Dict[str, str]:
    """Read cached validators for ``url``; return ``{}`` if absent or unreadable.

    A missing/corrupt sidecar must never break a refresh: we simply fall back to
    an unconditional download, which still works (just costs one body).
    """
    path = validators_path_for_url(meta_dir, url)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only the string-valued keys we understand.
    out: Dict[str, str] = {}
    for key in ("etag", "last_modified"):
        val = data.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    return out


def write_validators(
    meta_dir: Path,
    url: str,
    etag: Optional[str],
    last_modified: Optional[str],
) -> None:
    """Persist the server validators for ``url`` (atomic, best-effort).

    Only the headers the server actually sent are stored. Written to a ``.part``
    temp file and ``os.replace``d in, mirroring the atomic-download invariant so
    a crash mid-write never leaves a corrupt sidecar.
    """
    payload: Dict[str, str] = {}
    if etag:
        payload["etag"] = etag
    if last_modified:
        payload["last_modified"] = last_modified

    path = validators_path_for_url(meta_dir, url)
    try:
        meta_dir.mkdir(parents=True, exist_ok=True)
        if not payload:
            # Nothing to remember (server sent no validators); drop any stale
            # sidecar so we don't send outdated conditional headers next time.
            if path.exists():
                path.unlink()
            return
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # never let validator caching break a refresh
        print(f"[WARN] Could not store validators for {url}: {exc}", file=sys.stderr)


def fetch_list_conditional(url: str, dest_dir: Path, meta_dir: Path) -> str:
    """Conditionally download a remote URL-list file.

    Uses cached ETag / Last-Modified validators (when both a cached copy and a
    sidecar exist) to send If-None-Match / If-Modified-Since, so the server can
    answer "304 Not Modified" without resending the body. Only when the remote
    copy is genuinely newer is the body downloaded, the cache atomically
    replaced, and an ``[INFO]`` "remote newer" line logged.

    Returns one of:
    - ``"updated"``   - a new body was downloaded and the cache replaced,
    - ``"unchanged"`` - the remote copy matched the cache (304, or identical
      bytes from a server that sends no validators); the cache is kept,
    - ``"cached"``    - the download failed but a cached copy exists; kept.

    Raises RuntimeError if the download fails AND no cached copy exists, matching
    fetch_list()'s contract.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / list_filename_for_url(url)
    tmp = dest.with_name(dest.name + ".part")

    had_cache = dest.exists()
    # Conditional headers only make sense when we actually have something cached
    # to compare against; otherwise we must fetch the full body anyway.
    headers: Dict[str, str] = {}
    if had_cache:
        validators = read_validators(meta_dir, url)
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        if validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]

    content: Optional[bytes] = None
    new_etag: Optional[str] = None
    new_last_modified: Optional[str] = None
    try:
        resp = requests.get(
            url,
            timeout=LISTS_FETCH_TIMEOUT,
            allow_redirects=True,
            headers=headers or None,
        )
        # 304 must be handled before raise_for_status(): the server is telling us
        # the cached copy is still current, so there is nothing to download.
        if getattr(resp, "status_code", None) == 304 and had_cache:
            return "unchanged"
        resp.raise_for_status()
        resp_headers = getattr(resp, "headers", {}) or {}
        new_etag = resp_headers.get("ETag")
        new_last_modified = resp_headers.get("Last-Modified")
        body = resp.content
        # An empty body is treated as a failed download so it never replaces a
        # good cached list (mirrors fetch_list()).
        if body:
            content = body
    except Exception as exc:  # network error, timeout, HTTP error, etc.
        print(f"[WARN] Download error for {url}: {exc}", file=sys.stderr)
        content = None

    if content is not None:
        # Some servers send no validators; in that case a 200 always carries a
        # body. Compare against the cache so we don't falsely announce "newer"
        # when the bytes are actually identical.
        if had_cache:
            try:
                if dest.read_bytes() == content:
                    # Still refresh the validators so future requests can 304.
                    write_validators(meta_dir, url, new_etag, new_last_modified)
                    return "unchanged"
            except Exception:
                # If we cannot read the cache for comparison, treat as changed.
                pass

        try:
            tmp.write_bytes(content)
            os.replace(tmp, dest)
        except Exception as exc:
            print(f"[WARN] Failed to store downloaded list {url}: {exc}", file=sys.stderr)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            # Fall through to the cache-or-raise handling below.
        else:
            write_validators(meta_dir, url, new_etag, new_last_modified)
            if had_cache:
                print(f"[INFO] {url}: remote newer than local cache; updated {dest}.")
            else:
                print(f"[INFO] Downloaded {url} (initial).")
            return "updated"

    # Download failed (or could not be stored). Clean up any partial file.
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    if dest.exists():
        print(
            f"[WARN] Could not download {url}; using cached copy at {dest}.",
            file=sys.stderr,
        )
        return "cached"

    raise RuntimeError(f"Failed to download {url} and no cached copy exists.")


def refresh_lists_conditional(
    list_urls: List[str],
    dest_dir: Path,
    meta_dir: Path,
) -> Tuple[int, int]:
    """Conditionally refresh all remote URL-list files.

    Returns a ``(updated, unchanged)`` tuple: how many lists were freshly
    downloaded because the remote copy was newer, and how many were left as-is
    (304 / identical bytes / kept cache on failure).

    A failure on any single list is fatal only when that list has no cached
    copy (fetch_list_conditional raises); otherwise the cached copy is kept.
    """
    updated = 0
    unchanged = 0
    for url in list_urls:
        if fetch_list_conditional(url, dest_dir, meta_dir) == "updated":
            updated += 1
        else:
            unchanged += 1
    return updated, unchanged


def load_all_urls(
    lists_dir: Path,
    extra_urls_path: Optional[Path] = None,
) -> List[URLConfig]:
    """Assemble the full pool of video URLConfigs for the rotation loop.

    Combines:
    - every cached "*.txt" list in lists_dir (downloaded from --config), and
    - the optional --urls file (extra_urls_path), if given.

    Raises ValueError if the combined pool is empty.
    """
    urls: List[URLConfig] = []

    if lists_dir.exists():
        for txt in sorted(lists_dir.glob("*.txt")):
            try:
                urls.extend(load_urls(txt))
            except Exception as exc:
                # A single broken/empty cached list must not take down the pool.
                print(f"[WARN] Skipping list '{txt}': {exc}", file=sys.stderr)

    if extra_urls_path is not None:
        urls.extend(load_urls(extra_urls_path))

    if not urls:
        raise ValueError(
            "No URLs available: downloaded lists are empty/missing and no "
            "--urls file was provided."
        )
    return urls


# -------------------------
# Main
# -------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate YouTube URLs in Chrome (player fullscreen, background preload)."
    )
    parser.add_argument(
        "--config",
        required=True,
        help=(
            "Path to a .conf file listing remote URL-list files to download "
            "(one URL per line, e.g. raw.githubusercontent.com/.../jazz.txt). "
            "These are re-downloaded periodically in the background."
        ),
    )
    parser.add_argument(
        "--urls",
        type=str,
        default=None,
        help=(
            "Optional path to a LOCAL text file with YouTube URLs (one per "
            "line, with optional prefixes/suffixes). Its links are added to "
            "those from the downloaded lists."
        ),
    )
    parser.add_argument(
        "--lists-refresh-interval",
        type=int,
        default=DEFAULT_LISTS_REFRESH_INTERVAL,
        help=(
            "How often (seconds) to re-download the URL-list files from "
            f"--config in the background. Default: {DEFAULT_LISTS_REFRESH_INTERVAL}."
        ),
    )
    parser.add_argument(
        "--lists-dir",
        type=str,
        default=None,
        help=(
            "Directory for cached downloaded lists "
            f"(default: ./{DEFAULT_LISTS_DIR_NAME})."
        ),
    )
    parser.add_argument(
        "--validators-dir",
        type=str,
        default=None,
        help=(
            "Directory for cached HTTP conditional-request validators "
            "(ETag/Last-Modified), used so lists are only re-downloaded when the "
            "remote copy is newer. Default: a per-system temp dir "
            f"(<tempdir>/{DEFAULT_VALIDATORS_DIR_NAME})."
        ),
    )
    parser.add_argument(
        "--min-delay",
        type=int,
        default=DEFAULT_MIN_DELAY,
        help="Minimum delay between switches (seconds).",
    )
    parser.add_argument(
        "--max-delay",
        type=int,
        default=DEFAULT_MAX_DELAY,
        help="Maximum delay between switches (seconds).",
    )
    parser.add_argument(
        "--handoff-delay",
        type=int,
        default=0,
        help="Extra seconds to wait AFTER next page has loaded, before swapping.",
    )
    parser.add_argument(
        "--chrome",
        type=str,
        default=None,
        help="Path to chrome.exe. If omitted, common paths/PATH are tried.",
    )
    parser.add_argument(
        "--remote-port",
        type=int,
        default=9222,
        help="Chrome remote debugging port to use. Default: 9222",
    )
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help=(
            "Custom Chrome user-data-dir root for private profile "
            "(default: ./chrome_profile). Ignored if --use-default-profile."
        ),
    )
    parser.add_argument(
        "--use-default-profile",
        action="store_true",
        help=(
            "Use your normal Chrome profile (stay logged in). "
            "Close Chrome first or use --force-close-chrome."
        ),
    )
    parser.add_argument(
        "--profile-directory",
        type=str,
        default="Default",
        help="Profile directory name inside 'User Data' (e.g., 'Default', 'Profile 1').",
    )
    parser.add_argument(
        "--force-close-chrome",
        action="store_true",
        help=(
            "When using --use-default-profile, force-close existing chrome.exe "
            "before starting."
        ),
    )
    parser.add_argument(
        "--mute",
        action="store_true",
        help="Keep the video muted (recommended for autoplay reliability).",
    )
    parser.add_argument(
        "--player-fullscreen",
        action="store_true",
        help="Toggle the YouTube player to fullscreen (not OS fullscreen).",
    )

    args = parser.parse_args()

    # Validate delay values: enforce a sane lower bound and ordering so the
    # random.randint(min, max) call below never gets an inverted range.
    if args.min_delay < 10 or args.max_delay < args.min_delay:
        print(
            "Invalid delay values. Example: --min-delay 120 --max-delay 600",
            file=sys.stderr,
        )
        sys.exit(2)

    # Add a small lower bound on the refresh interval too.
    if args.lists_refresh_interval < 10:
        print(
            "Invalid --lists-refresh-interval (minimum 10 seconds).",
            file=sys.stderr,
        )
        sys.exit(2)

    config_path = Path(args.config)
    base_dir = Path.cwd()
    lists_dir = Path(args.lists_dir) if args.lists_dir else base_dir / DEFAULT_LISTS_DIR_NAME
    validators_dir = Path(args.validators_dir) if args.validators_dir else default_validators_dir()
    extra_urls_path = Path(args.urls) if args.urls else None

    # Read the list of remote URL-list files to keep in sync.
    try:
        list_urls: List[str] = parse_lists_config(config_path)
    except Exception as exc:
        print(f"[ERROR] Failed to read --config '{config_path}': {exc}", file=sys.stderr)
        sys.exit(1)

    # Per the design: start immediately and only refresh in the background.
    # We still attempt one download up front so a first-ever run has content,
    # but we tolerate failures here as long as *some* usable URLs exist (either
    # from a previous cache or from --urls). A hard failure is raised by
    # load_all_urls() below only if the pool ends up empty.
    try:
        refresh_lists_conditional(list_urls, lists_dir, validators_dir)
    except Exception as exc:
        print(f"[WARN] Initial list download incomplete: {exc}", file=sys.stderr)

    # Build the initial URL pool (downloaded lists + optional --urls file).
    try:
        urls: List[URLConfig] = load_all_urls(lists_dir, extra_urls_path)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    # Background thread: periodically re-download the lists. It only refreshes
    # the cached files on disk; the main loop re-reads them via load_all_urls()
    # on every switch, so no shared in-memory state needs locking.
    stop_event = threading.Event()

    def lists_refresher() -> None:
        while not stop_event.wait(args.lists_refresh_interval):
            try:
                updated, unchanged = refresh_lists_conditional(
                    list_urls, lists_dir, validators_dir
                )
                print(
                    f"[INFO] Refreshed URL lists ({updated} updated, "
                    f"{unchanged} unchanged)."
                )
            except Exception as exc:
                # Never let a refresh failure kill the thread or the program;
                # the cached copies remain usable.
                print(f"[WARN] List refresh failed: {exc}", file=sys.stderr)

    refresher_thread = threading.Thread(target=lists_refresher, daemon=True)
    refresher_thread.start()

    # Locate Chrome
    try:
        chrome_exe = find_chrome_exe(args.chrome)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    # Determine which Chrome profile root to use
    if args.use_default_profile:
        # Windows default user data dir
        profile_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
        if not profile_root.exists():
            print(f"[ERROR] Chrome user data dir not found at {profile_root}", file=sys.stderr)
            sys.exit(1)

        if is_chrome_running():
            if args.force_close_chrome:
                try:
                    subprocess.run(
                        ["taskkill", "/IM", "chrome.exe", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    time.sleep(1.0)
                except Exception as exc:
                    print(f"[WARN] Failed to force-close Chrome: {exc}", file=sys.stderr)
            else:
                print(
                    "Chrome is running. Close all Chrome windows "
                    "or rerun with --force-close-chrome.",
                    file=sys.stderr,
                )
                sys.exit(1)

        user_data_dir = profile_root
        profile_directory = args.profile_directory
    else:
        # Use a private profile so we don't touch your main Chrome
        if args.profile_dir:
            user_data_dir = Path(args.profile_dir)
        else:
            user_data_dir = base_dir / "chrome_profile"
        try:
            user_data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"[ERROR] Failed to create profile directory {user_data_dir}: {exc}", file=sys.stderr)
            sys.exit(1)
        profile_directory = None

    print(f"[INFO] Using Chrome user-data-dir: {user_data_dir}")
    if profile_directory:
        print(f"[INFO] Using profile directory: {profile_directory}")

    chrome_proc: Optional[subprocess.Popen[Any]] = None
    controller: Optional[ChromeController] = None
    managed_windows: List[ManagedWindow] = []
    prev: Optional[ManagedWindow] = None
    prev_url: Optional[str] = None
    prev_cfg: Optional[URLConfig] = None
    prev_template: Optional[str] = None

    def cleanup_all() -> None:
        """Close our managed windows and terminate the Chrome we started."""
        nonlocal chrome_proc, controller, managed_windows
        try:
            if controller is not None:
                for mw in managed_windows:
                    try:
                        controller.close_target(mw.target_id)
                    except Exception:
                        pass
                    try:
                        mw.page_conn.close()
                    except Exception:
                        pass
        finally:
            if chrome_proc is not None:
                try:
                    chrome_proc.terminate()
                except Exception:
                    pass

    try:
        # Start Chrome and connect to CDP
        chrome_proc = start_chrome(chrome_exe, args.remote_port, user_data_dir, profile_directory=profile_directory)
        controller = ChromeController(args.remote_port)

        # INITIAL: pick a URL and show it (maximized window + player fullscreen if requested)
        current_cfg = choose_url(urls, exclude_template=None)
        current_url = ensure_query_param(render_url(current_cfg), "autoplay", "1")
        target_id = controller.create_new_window_target()
        mw = controller.connect_page(target_id)
        controller.navigate_and_wait(mw, current_url, timeout=90.0)
        controller.activate_target(mw.target_id)
        controller.set_window_state(mw.window_id, "maximized")
        controller.try_autoplay_and_fullscreen_player(
            mw,
            player_fullscreen=args.player_fullscreen,
            mute=args.mute,
        )

        managed_windows = [mw]
        prev = mw
        prev_url = current_url
        prev_cfg = current_cfg
        prev_template = current_cfg.template

        print(
            f"[INFO] Showing initial URL "
            f"(player_fullscreen={args.player_fullscreen}, mute={args.mute}): {current_url}"
        )

        # MAIN LOOP
        #
        # Each iteration: wait the (random + extra) delay, then preload the next
        # video in a hidden background window before tearing down the current
        # one. Loading the replacement first keeps the screen from flashing an
        # empty window during the switch; only once it is ready do we close the
        # old window and bring the new one to the foreground.
        while True:
            if prev_cfg is None:
                # Should not happen (prev_cfg is set right after the initial
                # load), but fall back to a plain random delay just in case.
                base_delay = random.randint(args.min_delay, args.max_delay)
                extra = 0
            else:
                base_delay = random.randint(args.min_delay, args.max_delay)
                extra = prev_cfg.extra_delay

            delay = base_delay + extra
            if extra > 0:
                print(
                    f"[INFO] Waiting {delay} seconds before preloading next URL "
                    f"(base={base_delay}s + extra={extra}s from config)..."
                )
            else:
                print(f"[INFO] Waiting {delay} seconds before preloading next URL...")
            time.sleep(delay)

            # Re-read the URL pool so changes are picked up without restarting.
            # The cached list files on disk are kept current by the background
            # refresher thread; here we just re-read whatever is on disk plus
            # the optional --urls file.
            try:
                urls = load_all_urls(lists_dir, extra_urls_path)
            except Exception as exc:
                print(
                    f"[WARN] Could not reload URL pool: {exc}. "
                    f"Using previously loaded URLs.",
                    file=sys.stderr,
                )
                if not urls:
                    raise

            # Pick next URL (not equal to previous template if possible)
            next_cfg = choose_url(urls, exclude_template=prev_template)
            next_url = ensure_query_param(render_url(next_cfg), "autoplay", "1")
            print(f"[INFO] Preloading next URL (muted, minimized): {next_url}")

            # Create background window minimized
            next_target = controller.create_new_window_target()
            next_mw = controller.connect_page(next_target)
            try:
                controller.set_window_state(next_mw.window_id, "minimized")
            except Exception:
                pass

            # Navigate & wait for load
            controller.navigate_and_wait(next_mw, next_url, timeout=90.0)

            # Keep it muted while in background to avoid double audio
            controller.try_autoplay_and_fullscreen_player(
                next_mw,
                player_fullscreen=False,
                mute=True,
            )

            # Optional extra delay before swap (handoff)
            if args.handoff_delay > 0:
                print(
                    f"[INFO] Handoff delay: waiting {args.handoff_delay} seconds "
                    f"before swap..."
                )
                time.sleep(args.handoff_delay)

            # Swap
            print("[INFO] Swapping windows...")
            if prev is not None:
                try:
                    controller.close_target(prev.target_id)
                except Exception as exc:
                    print(f"[WARN] Failed to close previous window: {exc}", file=sys.stderr)
                try:
                    prev.page_conn.close()
                except Exception:
                    pass

            # Promote next
            controller.activate_target(next_mw.target_id)
            controller.set_window_state(next_mw.window_id, "maximized")
            controller.try_autoplay_and_fullscreen_player(
                next_mw,
                player_fullscreen=args.player_fullscreen,
                mute=args.mute,  # <- unmute if you didn't pass --mute
            )

            managed_windows = [next_mw]
            prev = next_mw
            prev_url = next_url
            prev_cfg = next_cfg
            prev_template = next_cfg.template

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user. Exiting...")
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
    finally:
        stop_event.set()  # signal the background refresher to stop
        cleanup_all()


if __name__ == "__main__":
    main()
