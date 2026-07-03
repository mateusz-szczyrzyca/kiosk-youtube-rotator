# kiosk_youtube_rotator

A tiny, unattended **YouTube rotator** for a TV or any spare screen.

You give it **one active playlist** of YouTube links (`--urls`). Every so often
it picks a **random** link from that list, opens it in a fullscreen,
CDP-controlled Chrome window, and lets it play. After a random amount of time it
picks another link, loads it in the background, and swaps it in — closing the
old window and bringing the new video forward. Then it repeats, forever.

The result is a screen that just *plays something*, hands-free. No remote, no
playlist babysitting, no "the stream ended an hour ago and now it's just a black
screen." If a link happens to be dead (a webcam that got taken offline, say),
that's fine — nothing plays for a while, and the next rotation quietly replaces
it.

The list you play can itself be **kept fresh from a remote URL** (e.g. a raw
GitHub file): a background thread re-downloads the `--config` lists in place
whenever the remote copy is newer, so you can edit them remotely without
restarting — and without depending on git being installed on the kiosk box.
Only the single `--urls` list is ever played, so different genres never get
mixed together.

## Why

I run this on a TV wired to a laptop as a plain external monitor — none of the
TV's "smart" features are involved. I keep a couple of playlists:

- one with **live nest-box / wildlife / nature cams**, and
- one with **long relaxing jazz recordings**.

I don't have to touch anything. I always know that sooner or later the picture
will change and "something new" will come on.

## How it works

- Plays exactly **one active list**, the **`--urls` file** (required). The
  rotation loop draws random links from that single list — nothing else is mixed
  in.
- Reads a **`--config` file** that lists *remote playlist URLs* (one per line)
  to keep fresh. A **background thread** re-downloads each one every few minutes
  (default 5; see `--lists-refresh-interval`), writing it **in place by its
  basename** (e.g. `music-jazz-list.txt`) into `--lists-dir` (default: the
  working directory). The refresh uses **HTTP conditional requests** (`ETag` /
  `Last-Modified`), so a list is only re-downloaded when the remote copy is
  actually **newer** — and when that happens it's logged, e.g.
  `[INFO] https://.../cams.txt: remote newer than local cache; updated ...`.
  The validators it remembers between checks live in a per-system temp directory
  (see `--validators-dir`); losing them only costs one extra full download.
- If your `--urls` file is one of the `--config` lists (same basename), it is
  kept fresh automatically. If it isn't, it's simply played as-is.
- A list the program downloaded in a previous run that you later remove from
  `--config` is **deleted**; files the program never created are never touched.
- Launches Chrome in kiosk mode with the **Chrome DevTools Protocol (CDP)**
  enabled on a local debugging port. Control happens over a CDP websocket
  rather than fragile OS-level key presses.
- Opens a link, maximizes the window, and (optionally) toggles the **YouTube
  player's** fullscreen — the in-page player fullscreen, not an OS-level one.
- For each switch it **preloads the next link in a hidden background window**
  first; only once that window is ready does it close the current one and
  promote the new window. This avoids flashing an empty window during the swap.
- The active `--urls` list is **re-read on every switch**, so remote edits
  (picked up on the next refresh) and local edits take effect without a restart.

### Resilience

- If a list **download fails but a local copy exists**, the local copy is kept
  and a warning is logged.
- If the **`--urls` file cannot be read** (missing/empty) at startup, the
  program exits with an error rather than spinning on an empty pool.
- Refreshes are **conditional**: when the remote copy is unchanged the server
  replies `304 Not Modified` and the local copy is kept untouched. Servers that
  send no validators are handled too — the freshly downloaded bytes are compared
  against the local file, so an identical body is never re-announced as "newer".
- Stale-list cleanup only ever deletes files the program itself created (tracked
  in a metadata manifest), so your own local files are never removed.

> **Platform:** Chrome control is built around Windows (process handling,
> `tasklist`/`taskkill`, the default profile path). The parsing, downloading and
> URL-handling logic is platform-independent and unit-tested everywhere, but the
> rotator itself is intended to run on Windows 10/11.

## Requirements

- Windows 10/11
- Google Chrome
- Python 3.8+
- Python packages:

  ```bash
  pip install requests websocket-client
  ```

URL-list files are downloaded with the `requests` package (already required
above), so **no external command-line downloader** (wget/curl) needs to be
installed on the kiosk box.

## Quick start

1. Create a `--config` file, e.g. `lists-urls.conf`, with one remote playlist
   URL per line (these are kept fresh, but only the `--urls` one plays):

   ```text
   # remote playlists (raw GitHub links work well)
   https://raw.githubusercontent.com/<you>/<repo>/main/music-jazz-list.txt
   https://raw.githubusercontent.com/<you>/<repo>/main/live-cams-wildfire.txt
   ```

2. Run, choosing the single list you want to play with `--urls`:

   ```bash
   python kiosk_youtube_rotator.py --config lists-urls.conf --urls music-jazz-list.txt --player-fullscreen --mute
   ```

   Because `music-jazz-list.txt` is also one of the `--config` lists, it's kept
   fresh in place while it plays. Want cams instead? Just point `--urls` at
   `live-cams-wildfire.txt` — the two never get mixed.

3. Press `Ctrl+C` to stop. The script stops the background refresher, closes the
   windows it opened, and terminates the Chrome instance it started.

`--mute` is recommended for reliable autoplay; drop it if you want sound (handy
for the jazz list, less so for wildlife cams).

By default the rotator forces **livestreams** to the live head each time they
come to the foreground, so a live cam never lingers on a buffered/old DVR
position (YouTube can otherwise resume a stream minutes — or a day — behind
live). This only affects genuine livestreams; regular videos, including `%s`
random-start links, keep their chosen start position. Pass `--no-force-live` to
turn it off and allow normal DVR behavior.

## Input files

There are **two kinds** of input file, with two different formats.

### 1. The `--config` file (remote lists to keep fresh)

One **playlist URL** per line. Blank lines and lines starting with `#` are
ignored. Each URL must point to a file in the *playlist format* below, and is
written in place by its basename (e.g. `music-jazz-list.txt`). Example:

```text
# kept fresh in the background; whichever one --urls names is what plays
https://raw.githubusercontent.com/<you>/<repo>/main/music-jazz-list.txt
https://raw.githubusercontent.com/<you>/<repo>/main/live-cams-wildfire.txt
```

### 2. Playlist files (`--urls`, the active list, and each downloaded list)

One **video link** per line. Blank lines and lines starting with `#` are
ignored.

| Line | Meaning |
|------|---------|
| `link` | Plain link. Plays, then switches after a random delay in `[--min-delay, --max-delay]`. |
| `link?t=%s:200` | `%s` + trailing `:N` → `%s` is replaced by a random integer `0..N` seconds. **Randomizes the start position** of the video. |
| `300:link` | Leading `N:` → play this one **`N` extra seconds** on top of the random delay. |
| `300:link?start=%s:200` | Both at once: +300s of playback **and** a random start in `0..200`. |

Notes:

- The trailing `:N` start-offset is **only** applied when the line contains
  `%s`. A normal URL that happens to end in `:digits` is left untouched.
- The same link is never picked twice in a row (unless it's the only link).

Example playlist file:

```text
# relaxing jazz — long videos, random start so it's different each time
https://youtu.be/zOBdWVCTNco?t=%s:41382
https://youtu.be/pq3fC9Oc-uo?t=%s:8927

# a webcam I want to linger on a bit longer
300:https://www.youtube.com/watch?v=l0nT3wSnxEo

# a plain link, default behaviour
https://www.youtube.com/watch?v=ygedg03NpUQ
```

## Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `--config PATH` | *(required)* | File listing remote list URLs to keep fresh (one per line). Does not decide what plays. |
| `--urls PATH` | *(required)* | The single ACTIVE playlist file that is played. Kept fresh automatically if its basename matches a `--config` list. |
| `--lists-refresh-interval SEC` | `300` | How often to re-download the `--config` lists in the background. |
| `--lists-dir PATH` | working dir | Directory where `--config` lists are written in place by basename. |
| `--validators-dir PATH` | per-system temp dir | Where to cache HTTP conditional-request validators (ETag/Last-Modified) so lists are only re-downloaded when the remote is newer. |
| `--min-delay SEC` | `120` | Minimum delay between switches. |
| `--max-delay SEC` | `600` | Maximum delay between switches. |
| `--handoff-delay SEC` | `0` | Extra wait *after* the next page has loaded, before swapping. |
| `--chrome PATH` | auto | Path to `chrome.exe`. Common locations and `PATH` are tried if omitted. |
| `--remote-port N` | `9222` | Chrome remote-debugging port. |
| `--player-fullscreen` | off | Toggle the YouTube player to fullscreen. |
| `--force-live` / `--no-force-live` | on | For livestreams, seek to the live head instead of a buffered/resumed position. No-op for regular videos (including `%s` random-start links). |
| `--mute` | off | Keep the video muted (recommended for autoplay). |
| `--profile-dir PATH` | `./chrome_profile` | User-data-dir for the private profile. Ignored with `--use-default-profile`. |
| `--use-default-profile` | off | Use your normal Chrome profile (stay logged in). Close Chrome first or use `--force-close-chrome`. |
| `--profile-directory NAME` | `Default` | Profile directory name inside *User Data* (e.g. `Default`, `Profile 1`). |
| `--force-close-chrome` | off | With `--use-default-profile`, force-close running Chrome first. |

By default the script uses a **separate, private Chrome profile** in
`./chrome_profile`, so it never touches your everyday Chrome. Use
`--use-default-profile` only if you specifically need to be logged in (e.g. for
membership-only streams); note that this requires closing your normal Chrome.

## Running the tests

```bash
pip install pytest
pytest -q
```

The test suite covers the pure logic — playlist-config parsing, the
list-download layer (cache fallback, hard-failure, HTTP/network errors, empty
responses — all with `requests.get` mocked), local-filename derivation,
the combined URL pool, plus config-line parsing, file
loading, random link selection, URL rendering, query-string handling, and Chrome
executable lookup. The CDP/browser-control layer and the main loop are not
unit-tested, as they need a live browser; that's integration-test territory.

## License

MIT — see [LICENSE](LICENSE).
