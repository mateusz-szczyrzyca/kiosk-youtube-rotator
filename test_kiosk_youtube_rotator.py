#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for rotate_youtube.py

Scope
-----
These tests cover the pure, deterministic logic of the script: config-line
parsing, file loading, URL selection/rendering and query-string handling, plus
the Chrome executable lookup. The Chrome DevTools machinery (CDPConnection,
ChromeController) and the main() loop are intentionally NOT unit-tested here:
they require a live browser and a websocket connection, which belongs in an
integration test rather than a unit test.

Run with:
    pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiosk_youtube_rotator as ry


# ---------------------------------------------------------------------------
# parse_config_line
# ---------------------------------------------------------------------------


class TestParseConfigLine:
    def test_plain_link(self):
        cfg = ry.parse_config_line("https://youtu.be/abc")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc"
        assert cfg.extra_delay == 0
        assert cfg.max_start_offset is None

    def test_trailing_random_start_offset(self):
        # "%s" present + trailing ":N" -> N becomes max_start_offset
        cfg = ry.parse_config_line("https://youtu.be/abc?t=%s:200")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc?t=%s"
        assert cfg.extra_delay == 0
        assert cfg.max_start_offset == 200

    def test_leading_extra_delay(self):
        # leading "N:" -> N becomes extra_delay, no random start
        cfg = ry.parse_config_line("300:https://youtu.be/abc")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc"
        assert cfg.extra_delay == 300
        assert cfg.max_start_offset is None

    def test_both_prefix_and_suffix(self):
        # both leading extra_delay and trailing start offset
        cfg = ry.parse_config_line("300:https://youtu.be/abc?start=%s:200")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc?start=%s"
        assert cfg.extra_delay == 300
        assert cfg.max_start_offset == 200

    def test_trailing_number_ignored_without_placeholder(self):
        # No "%s" in the URL -> trailing ":NNN" must be left untouched, because
        # a real YouTube URL can legitimately end with ":<digits>".
        cfg = ry.parse_config_line("https://youtu.be/abc:200")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc:200"
        assert cfg.max_start_offset is None

    @pytest.mark.parametrize("line", ["", "   ", "\n", "\t  \n"])
    def test_blank_lines_return_none(self, line):
        assert ry.parse_config_line(line) is None

    @pytest.mark.parametrize("line", ["# a comment", "   # indented comment"])
    def test_comment_lines_return_none(self, line):
        assert ry.parse_config_line(line) is None

    def test_surrounding_whitespace_is_stripped(self):
        cfg = ry.parse_config_line("   https://youtu.be/abc   \n")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc"

    def test_real_world_youtu_be_line(self):
        # A line straight out of the user's jazz playlist file.
        cfg = ry.parse_config_line("https://youtu.be/zOBdWVCTNco?t=%s:41382")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/zOBdWVCTNco?t=%s"
        assert cfg.extra_delay == 0
        assert cfg.max_start_offset == 41382

    def test_real_world_full_watch_url(self):
        cfg = ry.parse_config_line(
            "https://www.youtube.com/watch?v=ZbX819P8aIY&t=%s:5000"
        )
        assert cfg is not None
        assert cfg.template == "https://www.youtube.com/watch?v=ZbX819P8aIY&t=%s"
        assert cfg.max_start_offset == 5000

    def test_zero_extra_delay_prefix(self):
        cfg = ry.parse_config_line("0:https://youtu.be/abc")
        assert cfg is not None
        assert cfg.template == "https://youtu.be/abc"
        assert cfg.extra_delay == 0


# ---------------------------------------------------------------------------
# load_urls
# ---------------------------------------------------------------------------


class TestLoadUrls:
    def test_loads_and_skips_comments_and_blanks(self, tmp_path: Path):
        content = (
            "# header comment\n"
            "\n"
            "https://youtu.be/one\n"
            "300:https://youtu.be/two\n"
            "   \n"
            "https://youtu.be/three?t=%s:100\n"
        )
        f = tmp_path / "list.txt"
        f.write_text(content, encoding="utf-8")

        urls = ry.load_urls(f)
        assert [u.template for u in urls] == [
            "https://youtu.be/one",
            "https://youtu.be/two",
            "https://youtu.be/three?t=%s",
        ]
        assert urls[1].extra_delay == 300
        assert urls[2].max_start_offset == 100

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            ry.load_urls(tmp_path / "does_not_exist.txt")

    def test_empty_file_raises_valueerror(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            ry.load_urls(f)

    def test_only_comments_raises_valueerror(self, tmp_path: Path):
        f = tmp_path / "comments.txt"
        f.write_text("# just\n# comments\n\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ry.load_urls(f)


# ---------------------------------------------------------------------------
# choose_url
# ---------------------------------------------------------------------------


class TestChooseUrl:
    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            ry.choose_url([])

    def test_single_url_returned(self):
        only = ry.URLConfig(template="https://youtu.be/only")
        assert ry.choose_url([only]) is only

    def test_excluded_template_is_avoided(self):
        a = ry.URLConfig(template="A")
        b = ry.URLConfig(template="B")
        # Excluding A means only B is eligible; over many draws we must never
        # get A back.
        for _ in range(50):
            assert ry.choose_url([a, b], exclude_template="A") is b

    def test_excluded_returned_when_its_the_only_option(self):
        # If the excluded template is the only entry, it should be returned
        # rather than raising or looping forever.
        a = ry.URLConfig(template="A")
        assert ry.choose_url([a], exclude_template="A") is a

    def test_returns_one_of_the_inputs(self, monkeypatch):
        a = ry.URLConfig(template="A")
        b = ry.URLConfig(template="B")
        c = ry.URLConfig(template="C")
        # Force random.choice to pick the middle element deterministically.
        monkeypatch.setattr(ry.random, "choice", lambda seq: seq[1])
        assert ry.choose_url([a, b, c]) is b


# ---------------------------------------------------------------------------
# render_url
# ---------------------------------------------------------------------------


class TestRenderUrl:
    def test_no_placeholder_returns_template_unchanged(self):
        cfg = ry.URLConfig(template="https://youtu.be/abc")
        assert ry.render_url(cfg) == "https://youtu.be/abc"

    def test_placeholder_without_offset_left_untouched(self):
        # "%s" present but max_start_offset is None -> no substitution attempted
        cfg = ry.URLConfig(template="https://youtu.be/abc?t=%s")
        assert ry.render_url(cfg) == "https://youtu.be/abc?t=%s"

    def test_placeholder_substituted_with_random_value(self, monkeypatch):
        monkeypatch.setattr(ry.random, "randint", lambda lo, hi: 123)
        cfg = ry.URLConfig(template="https://youtu.be/abc?t=%s", max_start_offset=200)
        assert ry.render_url(cfg) == "https://youtu.be/abc?t=123"

    def test_random_value_within_range(self):
        cfg = ry.URLConfig(template="https://youtu.be/abc?t=%s", max_start_offset=10)
        for _ in range(200):
            out = ry.render_url(cfg)
            sec = int(out.rsplit("=", 1)[1])
            assert 0 <= sec <= 10

    def test_randint_called_with_correct_bounds(self, monkeypatch):
        captured = {}

        def fake_randint(lo, hi):
            captured["lo"] = lo
            captured["hi"] = hi
            return lo

        monkeypatch.setattr(ry.random, "randint", fake_randint)
        cfg = ry.URLConfig(template="x=%s", max_start_offset=41382)
        ry.render_url(cfg)
        assert captured == {"lo": 0, "hi": 41382}

    def test_extra_percent_signs_do_not_crash(self, monkeypatch):
        # If the template has a stray "%" that breaks "%"-formatting, render_url
        # should swallow the TypeError and return the template unchanged rather
        # than raising.
        monkeypatch.setattr(ry.random, "randint", lambda lo, hi: 5)
        cfg = ry.URLConfig(template="https://x/%s?p=%bad", max_start_offset=10)
        # Should not raise; substitution is abandoned.
        assert ry.render_url(cfg) == "https://x/%s?p=%bad"


# ---------------------------------------------------------------------------
# ensure_query_param
# ---------------------------------------------------------------------------


class TestEnsureQueryParam:
    def test_adds_param_when_absent(self):
        out = ry.ensure_query_param("https://youtu.be/abc", "autoplay", "1")
        assert out == "https://youtu.be/abc?autoplay=1"

    def test_does_not_override_existing_param(self):
        out = ry.ensure_query_param(
            "https://youtu.be/abc?autoplay=0", "autoplay", "1"
        )
        assert "autoplay=0" in out
        assert "autoplay=1" not in out

    def test_preserves_other_params(self):
        out = ry.ensure_query_param(
            "https://www.youtube.com/watch?v=abc", "autoplay", "1"
        )
        # Both the original v= and the new autoplay= must be present.
        assert "v=abc" in out
        assert "autoplay=1" in out

    def test_idempotent(self):
        once = ry.ensure_query_param("https://youtu.be/abc", "autoplay", "1")
        twice = ry.ensure_query_param(once, "autoplay", "1")
        assert once == twice


# ---------------------------------------------------------------------------
# find_chrome_exe
# ---------------------------------------------------------------------------


class TestFindChromeExe:
    def test_user_supplied_path_is_preferred(self, tmp_path: Path):
        fake = tmp_path / "chrome.exe"
        fake.write_text("", encoding="utf-8")
        assert ry.find_chrome_exe(str(fake)) == str(fake)

    def test_user_supplied_expands_env_vars(self, tmp_path: Path, monkeypatch):
        fake = tmp_path / "chrome.exe"
        fake.write_text("", encoding="utf-8")
        monkeypatch.setenv("MY_CHROME_DIR", str(tmp_path))
        assert ry.find_chrome_exe("$MY_CHROME_DIR/chrome.exe") == str(fake)

    def test_falls_back_to_path_lookup(self, monkeypatch):
        # No user value, none of the hardcoded Windows paths exist here, so it
        # must consult shutil.which.
        monkeypatch.setattr(ry.os.path, "exists", lambda p: False)
        monkeypatch.setattr(
            ry.shutil, "which", lambda name: "/usr/bin/chrome" if name == "chrome" else None
        )
        assert ry.find_chrome_exe(None) == "/usr/bin/chrome"

    def test_raises_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(ry.os.path, "exists", lambda p: False)
        monkeypatch.setattr(ry.shutil, "which", lambda name: None)
        with pytest.raises(FileNotFoundError):
            ry.find_chrome_exe(None)

    def test_nonexistent_user_path_falls_through(self, monkeypatch, tmp_path: Path):
        # A user-supplied path that does not exist should not be returned; the
        # function should fall through to the other lookup strategies.
        missing = str(tmp_path / "nope" / "chrome.exe")
        monkeypatch.setattr(ry.shutil, "which", lambda name: "/usr/bin/chrome")
        # The hardcoded Windows candidates won't exist on this OS either.
        assert ry.find_chrome_exe(missing) == "/usr/bin/chrome"

    def test_finds_chromium_via_path(self, monkeypatch):
        # On Linux the binary is typically named chromium (not chrome), so the
        # PATH fallback must also try that name.
        monkeypatch.setattr(ry.os.path, "exists", lambda p: False)
        monkeypatch.setattr(
            ry.shutil,
            "which",
            lambda name: "/usr/bin/chromium" if name == "chromium" else None,
        )
        assert ry.find_chrome_exe(None) == "/usr/bin/chromium"

    def test_finds_google_chrome_absolute_candidate(self, monkeypatch):
        # A Linux absolute-path install location should be discovered directly.
        monkeypatch.setattr(
            ry.os.path,
            "exists",
            lambda p: p == "/usr/bin/google-chrome-stable",
        )
        monkeypatch.setattr(ry.shutil, "which", lambda name: None)
        assert ry.find_chrome_exe(None) == "/usr/bin/google-chrome-stable"


# ---------------------------------------------------------------------------
# start_chrome (launch flag construction)
# ---------------------------------------------------------------------------


class TestStartChrome:
    """Verify the platform-adapted launch flags without actually launching."""

    def _capture(self, monkeypatch) -> dict:
        captured: dict = {}

        class _FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

            def poll(self):
                return None

        monkeypatch.setattr(ry.subprocess, "Popen", _FakePopen)
        return captured

    def test_linux_disables_sandbox_by_default(self, monkeypatch, tmp_path: Path):
        captured = self._capture(monkeypatch)
        monkeypatch.setattr(ry.sys, "platform", "linux")
        monkeypatch.setattr(ry.os, "name", "posix")
        ry.start_chrome("/usr/bin/chromium", 9222, tmp_path)
        args = captured["args"]
        assert "--no-sandbox" in args
        assert "--disable-setuid-sandbox" in args
        assert "--ozone-platform-hint=auto" in args

    def test_linux_sandbox_can_be_forced_on(self, monkeypatch, tmp_path: Path):
        captured = self._capture(monkeypatch)
        monkeypatch.setattr(ry.sys, "platform", "linux")
        monkeypatch.setattr(ry.os, "name", "posix")
        ry.start_chrome("/usr/bin/chromium", 9222, tmp_path, no_sandbox=False)
        args = captured["args"]
        assert "--no-sandbox" not in args
        assert "--disable-setuid-sandbox" not in args
        # The Ozone hint is unrelated to the sandbox and stays on Linux.
        assert "--ozone-platform-hint=auto" in args

    def test_non_linux_has_no_linux_flags(self, monkeypatch, tmp_path: Path):
        # darwin stands in for any non-Linux platform: no sandbox/Ozone flags.
        captured = self._capture(monkeypatch)
        monkeypatch.setattr(ry.sys, "platform", "darwin")
        monkeypatch.setattr(ry.os, "name", "posix")
        ry.start_chrome("/usr/bin/chrome", 9222, tmp_path)
        args = captured["args"]
        assert "--no-sandbox" not in args
        assert "--ozone-platform-hint=auto" not in args

    def test_stderr_is_captured_to_log(self, monkeypatch, tmp_path: Path):
        captured = self._capture(monkeypatch)
        monkeypatch.setattr(ry.sys, "platform", "linux")
        monkeypatch.setattr(ry.os, "name", "posix")
        ry.start_chrome("/usr/bin/chromium", 9222, tmp_path)
        stderr_target = captured["kwargs"]["stderr"]
        # Chrome's stderr must go to a real log file, not be discarded, so a
        # startup crash can be diagnosed.
        assert stderr_target is not ry.subprocess.DEVNULL
        assert ry.chrome_stderr_log_path(tmp_path).exists()
        try:
            stderr_target.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# URLConfig dataclass defaults
# ---------------------------------------------------------------------------


class TestURLConfigDefaults:
    def test_defaults(self):
        cfg = ry.URLConfig(template="x")
        assert cfg.extra_delay == 0
        assert cfg.max_start_offset is None


# ---------------------------------------------------------------------------
# Round-trip: parse -> render
# ---------------------------------------------------------------------------


class TestParseRenderRoundTrip:
    def test_parsed_line_renders_to_concrete_url(self, monkeypatch):
        monkeypatch.setattr(ry.random, "randint", lambda lo, hi: 7)
        cfg = ry.parse_config_line("https://youtu.be/abc?t=%s:500")
        assert cfg is not None
        rendered = ry.render_url(cfg)
        assert rendered == "https://youtu.be/abc?t=7"

    def test_parsed_plain_line_renders_unchanged(self):
        cfg = ry.parse_config_line("https://youtu.be/abc")
        assert cfg is not None
        assert ry.render_url(cfg) == "https://youtu.be/abc"


# ---------------------------------------------------------------------------
# parse_lists_config
# ---------------------------------------------------------------------------


class TestParseListsConfig:
    def test_reads_urls_skipping_comments_and_blanks(self, tmp_path: Path):
        f = tmp_path / "lists.conf"
        f.write_text(
            "# remote playlists\n"
            "\n"
            "https://raw.githubusercontent.com/u/r/main/jazz.txt\n"
            "   https://raw.githubusercontent.com/u/r/main/cams.txt   \n"
            "# trailing comment\n",
            encoding="utf-8",
        )
        urls = ry.parse_lists_config(f)
        assert urls == [
            "https://raw.githubusercontent.com/u/r/main/jazz.txt",
            "https://raw.githubusercontent.com/u/r/main/cams.txt",
        ]

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            ry.parse_lists_config(tmp_path / "nope.conf")

    def test_empty_or_comment_only_raises(self, tmp_path: Path):
        f = tmp_path / "empty.conf"
        f.write_text("# only a comment\n\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ry.parse_lists_config(f)


# ---------------------------------------------------------------------------
# list_filename_for_url
# ---------------------------------------------------------------------------


class TestListFilenameForUrl:
    def test_keeps_basename_and_is_txt(self):
        name = ry.list_filename_for_url(
            "https://raw.githubusercontent.com/u/r/main/jazz.txt"
        )
        assert name.endswith("_jazz.txt")

    def test_deterministic(self):
        url = "https://example.com/a/list.txt"
        assert ry.list_filename_for_url(url) == ry.list_filename_for_url(url)

    def test_same_basename_different_url_no_collision(self):
        a = ry.list_filename_for_url("https://example.com/one/list.txt")
        b = ry.list_filename_for_url("https://example.com/two/list.txt")
        assert a != b
        assert a.endswith("_list.txt") and b.endswith("_list.txt")

    def test_appends_txt_when_missing(self):
        name = ry.list_filename_for_url("https://example.com/playlist")
        assert name.endswith("_playlist.txt")

    def test_sanitizes_unsafe_characters(self):
        name = ry.list_filename_for_url("https://example.com/a b?c=1.txt")
        # No spaces, "?", or "=" should survive in the filename.
        assert " " not in name and "?" not in name and "=" not in name


# ---------------------------------------------------------------------------
# fetch_list  (requests.get mocked)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for a requests.Response.

    `status` drives raise_for_status(): any value >= 400 raises, mirroring how
    requests rejects HTTP error responses so fetch_list() keeps the cache.
    `headers` carries response validators (ETag/Last-Modified) for the
    conditional-download path.
    """

    def __init__(
        self,
        content: bytes = b"",
        status: int = 200,
        headers: dict | None = None,
    ):
        self.content = content
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestFetchList:
    def test_successful_download_writes_cache(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"
        dest = tmp_path / ry.list_filename_for_url(url)

        monkeypatch.setattr(
            ry.requests,
            "get",
            lambda u, **k: _FakeResponse(b"https://youtu.be/abc\n"),
        )

        assert ry.fetch_list(url, tmp_path) is True
        assert dest.exists()
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/abc"
        # The .part temp file must have been renamed away.
        assert not (dest.with_name(dest.name + ".part")).exists()

    def test_failure_with_cache_keeps_old_and_returns_false(
        self, tmp_path: Path, monkeypatch
    ):
        url = "https://x/jazz.txt"
        dest = tmp_path / ry.list_filename_for_url(url)
        dest.write_text("https://youtu.be/cached\n", encoding="utf-8")

        # Server "fails" with an HTTP error -> raise_for_status() rejects it.
        monkeypatch.setattr(
            ry.requests, "get", lambda u, **k: _FakeResponse(b"oops", status=500)
        )

        assert ry.fetch_list(url, tmp_path) is False
        # Old cached content is untouched.
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/cached"

    def test_network_error_with_cache_keeps_old(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"
        dest = tmp_path / ry.list_filename_for_url(url)
        dest.write_text("https://youtu.be/cached\n", encoding="utf-8")

        def boom(u, **k):
            raise ry.requests.RequestException("connection refused")

        monkeypatch.setattr(ry.requests, "get", boom)

        assert ry.fetch_list(url, tmp_path) is False
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/cached"

    def test_failure_without_cache_raises(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"
        monkeypatch.setattr(
            ry.requests, "get", lambda u, **k: _FakeResponse(b"oops", status=404)
        )
        with pytest.raises(RuntimeError):
            ry.fetch_list(url, tmp_path)

    def test_empty_download_treated_as_failure(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"

        # 200 OK but an empty body -> must be treated as a failed download.
        monkeypatch.setattr(ry.requests, "get", lambda u, **k: _FakeResponse(b""))
        # No cache + empty result -> hard error.
        with pytest.raises(RuntimeError):
            ry.fetch_list(url, tmp_path)


# ---------------------------------------------------------------------------
# refresh_lists
# ---------------------------------------------------------------------------


class TestRefreshLists:
    def test_counts_fresh_downloads(self, tmp_path: Path, monkeypatch):
        calls = {"n": 0}

        def fake_fetch(url, dest_dir):
            calls["n"] += 1
            # First url "fresh", second "cached".
            return calls["n"] == 1

        monkeypatch.setattr(ry, "fetch_list", fake_fetch)
        fetched = ry.refresh_lists(["https://x/a.txt", "https://x/b.txt"], tmp_path)
        assert fetched == 1
        assert calls["n"] == 2

    def test_propagates_hard_failure(self, tmp_path: Path, monkeypatch):
        def fake_fetch(url, dest_dir):
            raise RuntimeError("no cache")

        monkeypatch.setattr(ry, "fetch_list", fake_fetch)
        with pytest.raises(RuntimeError):
            ry.refresh_lists(["https://x/a.txt"], tmp_path)


# ---------------------------------------------------------------------------
# read_validators / write_validators
# ---------------------------------------------------------------------------


class TestValidators:
    def test_round_trip(self, tmp_path: Path):
        url = "https://x/jazz.txt"
        ry.write_validators(tmp_path, url, etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
        got = ry.read_validators(tmp_path, url)
        assert got == {
            "etag": '"abc"',
            "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT",
        }

    def test_missing_returns_empty(self, tmp_path: Path):
        assert ry.read_validators(tmp_path, "https://x/missing.txt") == {}

    def test_corrupt_returns_empty(self, tmp_path: Path):
        url = "https://x/jazz.txt"
        path = ry.validators_path_for_url(tmp_path, url)
        path.write_text("{not json", encoding="utf-8")
        assert ry.read_validators(tmp_path, url) == {}

    def test_only_present_headers_are_stored(self, tmp_path: Path):
        url = "https://x/jazz.txt"
        ry.write_validators(tmp_path, url, etag='"only-etag"', last_modified=None)
        assert ry.read_validators(tmp_path, url) == {"etag": '"only-etag"'}

    def test_empty_validators_removes_stale_sidecar(self, tmp_path: Path):
        url = "https://x/jazz.txt"
        ry.write_validators(tmp_path, url, etag='"abc"', last_modified=None)
        # A subsequent response with no validators must drop the stale sidecar.
        ry.write_validators(tmp_path, url, etag=None, last_modified=None)
        assert ry.read_validators(tmp_path, url) == {}
        assert not ry.validators_path_for_url(tmp_path, url).exists()


# ---------------------------------------------------------------------------
# fetch_list_conditional  (requests.get mocked)
# ---------------------------------------------------------------------------


class TestFetchListConditional:
    def test_initial_download_writes_cache_and_validators(self, tmp_path: Path, monkeypatch, capsys):
        url = "https://x/jazz.txt"
        lists_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        dest = lists_dir / ry.list_filename_for_url(url)

        def fake_get(u, **k):
            # First request: no conditional headers should be sent yet.
            assert not k.get("headers")
            return _FakeResponse(
                b"https://youtu.be/abc\n",
                headers={"ETag": '"v1"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
            )

        monkeypatch.setattr(ry.requests, "get", fake_get)

        assert ry.fetch_list_conditional(url, lists_dir, meta_dir) == "updated"
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/abc"
        assert ry.read_validators(meta_dir, url) == {
            "etag": '"v1"',
            "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT",
        }
        out = capsys.readouterr().out
        assert "(initial)" in out
        assert "remote newer" not in out

    def test_not_modified_keeps_cache(self, tmp_path: Path, monkeypatch, capsys):
        url = "https://x/jazz.txt"
        lists_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        lists_dir.mkdir()
        dest = lists_dir / ry.list_filename_for_url(url)
        dest.write_text("https://youtu.be/cached\n", encoding="utf-8")
        ry.write_validators(meta_dir, url, etag='"v1"', last_modified=None)

        def fake_get(u, **k):
            # The stored ETag must be replayed as If-None-Match.
            assert k.get("headers", {}).get("If-None-Match") == '"v1"'
            return _FakeResponse(b"", status=304)

        monkeypatch.setattr(ry.requests, "get", fake_get)

        assert ry.fetch_list_conditional(url, lists_dir, meta_dir) == "unchanged"
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/cached"
        assert "remote newer" not in capsys.readouterr().out

    def test_changed_body_updates_and_logs_newer(self, tmp_path: Path, monkeypatch, capsys):
        url = "https://x/jazz.txt"
        lists_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        lists_dir.mkdir()
        dest = lists_dir / ry.list_filename_for_url(url)
        dest.write_text("https://youtu.be/old\n", encoding="utf-8")
        ry.write_validators(meta_dir, url, etag='"v1"', last_modified=None)

        def fake_get(u, **k):
            assert k.get("headers", {}).get("If-None-Match") == '"v1"'
            return _FakeResponse(b"https://youtu.be/new\n", headers={"ETag": '"v2"'})

        monkeypatch.setattr(ry.requests, "get", fake_get)

        assert ry.fetch_list_conditional(url, lists_dir, meta_dir) == "updated"
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/new"
        assert ry.read_validators(meta_dir, url) == {"etag": '"v2"'}
        assert "remote newer than local cache" in capsys.readouterr().out

    def test_identical_body_without_validators_is_unchanged(self, tmp_path: Path, monkeypatch, capsys):
        # A server that sends no validators always returns a 200 body. If the
        # bytes match the cache we must report "unchanged" and not claim "newer".
        url = "https://x/jazz.txt"
        lists_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        lists_dir.mkdir()
        dest = lists_dir / ry.list_filename_for_url(url)
        dest.write_text("https://youtu.be/same\n", encoding="utf-8")

        monkeypatch.setattr(
            ry.requests, "get", lambda u, **k: _FakeResponse(b"https://youtu.be/same\n")
        )

        assert ry.fetch_list_conditional(url, lists_dir, meta_dir) == "unchanged"
        assert "remote newer" not in capsys.readouterr().out

    def test_network_error_with_cache_returns_cached(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"
        lists_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        lists_dir.mkdir()
        dest = lists_dir / ry.list_filename_for_url(url)
        dest.write_text("https://youtu.be/cached\n", encoding="utf-8")

        def boom(u, **k):
            raise ry.requests.RequestException("connection refused")

        monkeypatch.setattr(ry.requests, "get", boom)

        assert ry.fetch_list_conditional(url, lists_dir, meta_dir) == "cached"
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/cached"

    def test_failure_without_cache_raises(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"
        monkeypatch.setattr(
            ry.requests, "get", lambda u, **k: _FakeResponse(b"oops", status=404)
        )
        with pytest.raises(RuntimeError):
            ry.fetch_list_conditional(url, tmp_path / "lists", tmp_path / "meta")

    def test_empty_body_without_cache_raises(self, tmp_path: Path, monkeypatch):
        url = "https://x/jazz.txt"
        monkeypatch.setattr(ry.requests, "get", lambda u, **k: _FakeResponse(b""))
        with pytest.raises(RuntimeError):
            ry.fetch_list_conditional(url, tmp_path / "lists", tmp_path / "meta")


# ---------------------------------------------------------------------------
# refresh_lists_conditional
# ---------------------------------------------------------------------------


class TestRefreshListsConditional:
    def test_counts_updated_and_unchanged(self, tmp_path: Path, monkeypatch):
        results = {"https://x/a.txt": "updated", "https://x/b.txt": "unchanged", "https://x/c.txt": "cached"}

        def fake_fetch(url, dest_dir, meta_dir):
            return results[url]

        monkeypatch.setattr(ry, "fetch_list_conditional", fake_fetch)
        updated, unchanged = ry.refresh_lists_conditional(
            list(results.keys()), tmp_path / "lists", tmp_path / "meta"
        )
        assert updated == 1
        assert unchanged == 2

    def test_propagates_hard_failure(self, tmp_path: Path, monkeypatch):
        def fake_fetch(url, dest_dir, meta_dir):
            raise RuntimeError("no cache")

        monkeypatch.setattr(ry, "fetch_list_conditional", fake_fetch)
        with pytest.raises(RuntimeError):
            ry.refresh_lists_conditional(["https://x/a.txt"], tmp_path / "lists", tmp_path / "meta")


# ---------------------------------------------------------------------------
# list_basename_for_url
# ---------------------------------------------------------------------------


class TestListBasenameForUrl:
    def test_keeps_basename_without_hash(self):
        name = ry.list_basename_for_url(
            "https://raw.githubusercontent.com/u/r/main/music-jazz-list.txt"
        )
        assert name == "music-jazz-list.txt"

    def test_same_basename_different_url_collides(self):
        # Without a hash prefix, two different URLs that share a basename map to
        # the same file (the refresher warns about this).
        a = ry.list_basename_for_url("https://example.com/one/list.txt")
        b = ry.list_basename_for_url("https://example.com/two/list.txt")
        assert a == b == "list.txt"

    def test_appends_txt_when_missing(self):
        assert ry.list_basename_for_url("https://example.com/playlist") == "playlist.txt"

    def test_sanitizes_unsafe_characters(self):
        name = ry.list_basename_for_url("https://example.com/a b?c=1.txt")
        assert " " not in name and "?" not in name and "=" not in name


# ---------------------------------------------------------------------------
# fetch_list_inplace  (requests.get mocked)
# ---------------------------------------------------------------------------


class TestFetchListInplace:
    def test_initial_download_writes_basename_file(self, tmp_path: Path, monkeypatch, capsys):
        url = "https://x/music-jazz-list.txt"
        base_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        dest = base_dir / "music-jazz-list.txt"

        monkeypatch.setattr(
            ry.requests,
            "get",
            lambda u, **k: _FakeResponse(b"https://youtu.be/abc\n", headers={"ETag": '"v1"'}),
        )

        assert ry.fetch_list_inplace(url, base_dir, meta_dir) == "updated"
        # File is written under its human basename, no hash prefix.
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/abc"
        assert "(initial)" in capsys.readouterr().out

    def test_not_modified_keeps_file(self, tmp_path: Path, monkeypatch):
        url = "https://x/music-jazz-list.txt"
        base_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        base_dir.mkdir()
        dest = base_dir / "music-jazz-list.txt"
        dest.write_text("https://youtu.be/cached\n", encoding="utf-8")
        ry.write_validators(meta_dir, url, etag='"v1"', last_modified=None)

        def fake_get(u, **k):
            assert k.get("headers", {}).get("If-None-Match") == '"v1"'
            return _FakeResponse(b"", status=304)

        monkeypatch.setattr(ry.requests, "get", fake_get)

        assert ry.fetch_list_inplace(url, base_dir, meta_dir) == "unchanged"
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/cached"

    def test_changed_body_updates_and_logs_newer(self, tmp_path: Path, monkeypatch, capsys):
        url = "https://x/music-jazz-list.txt"
        base_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        base_dir.mkdir()
        dest = base_dir / "music-jazz-list.txt"
        dest.write_text("https://youtu.be/old\n", encoding="utf-8")
        ry.write_validators(meta_dir, url, etag='"v1"', last_modified=None)

        monkeypatch.setattr(
            ry.requests,
            "get",
            lambda u, **k: _FakeResponse(b"https://youtu.be/new\n", headers={"ETag": '"v2"'}),
        )

        assert ry.fetch_list_inplace(url, base_dir, meta_dir) == "updated"
        assert dest.read_text(encoding="utf-8").strip() == "https://youtu.be/new"
        assert "remote newer than local cache" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# managed-lists manifest
# ---------------------------------------------------------------------------


class TestManagedManifest:
    def test_round_trip(self, tmp_path: Path):
        mapping = {"/lists/jazz.txt": "https://x/jazz.txt"}
        ry.write_managed_manifest(tmp_path, mapping)
        assert ry.read_managed_manifest(tmp_path) == mapping

    def test_missing_returns_empty(self, tmp_path: Path):
        assert ry.read_managed_manifest(tmp_path) == {}

    def test_corrupt_returns_empty(self, tmp_path: Path):
        ry.managed_manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert ry.read_managed_manifest(tmp_path) == {}


# ---------------------------------------------------------------------------
# refresh_config_lists
# ---------------------------------------------------------------------------


class TestRefreshConfigLists:
    def test_counts_updated_and_unchanged(self, tmp_path: Path, monkeypatch):
        results = {"https://x/a.txt": "updated", "https://x/b.txt": "unchanged"}

        def fake_fetch(url, base_dir, meta_dir):
            return results[url]

        monkeypatch.setattr(ry, "fetch_list_inplace", fake_fetch)
        updated, unchanged = ry.refresh_config_lists(
            list(results.keys()), tmp_path / "lists", tmp_path / "meta"
        )
        assert (updated, unchanged) == (1, 1)

    def test_writes_manifest_of_managed_files(self, tmp_path: Path, monkeypatch):
        base_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        monkeypatch.setattr(ry, "fetch_list_inplace", lambda u, b, m: "updated")

        ry.refresh_config_lists(["https://x/jazz.txt"], base_dir, meta_dir)
        manifest = ry.read_managed_manifest(meta_dir)
        assert manifest == {str(base_dir / "jazz.txt"): "https://x/jazz.txt"}

    def test_deletes_program_created_file_dropped_from_config(self, tmp_path: Path, monkeypatch):
        base_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        base_dir.mkdir()
        monkeypatch.setattr(ry, "fetch_list_inplace", lambda u, b, m: "unchanged")

        # First run manages both lists; second run drops cams from --config.
        ry.refresh_config_lists(
            ["https://x/jazz.txt", "https://x/cams.txt"], base_dir, meta_dir
        )
        cams = base_dir / "cams.txt"
        cams.write_text("https://youtu.be/cam\n", encoding="utf-8")

        ry.refresh_config_lists(["https://x/jazz.txt"], base_dir, meta_dir)

        # The dropped, program-created list is gone and off the manifest.
        assert not cams.exists()
        assert str(cams) not in ry.read_managed_manifest(meta_dir)

    def test_does_not_delete_files_it_never_created(self, tmp_path: Path, monkeypatch):
        base_dir = tmp_path / "lists"
        meta_dir = tmp_path / "meta"
        base_dir.mkdir()
        monkeypatch.setattr(ry, "fetch_list_inplace", lambda u, b, m: "unchanged")

        # A user's own file that the program never recorded in any manifest.
        user_file = base_dir / "my-own.txt"
        user_file.write_text("https://youtu.be/mine\n", encoding="utf-8")

        ry.refresh_config_lists(["https://x/jazz.txt"], base_dir, meta_dir)

        assert user_file.exists()

    def test_warns_on_duplicate_basename(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setattr(ry, "fetch_list_inplace", lambda u, b, m: "unchanged")
        ry.refresh_config_lists(
            ["https://a.com/list.txt", "https://b.com/list.txt"],
            tmp_path / "lists",
            tmp_path / "meta",
        )
        assert "share basename" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# load_all_urls
# ---------------------------------------------------------------------------


class TestLoadAllUrls:
    def test_combines_lists_dir_and_urls_file(self, tmp_path: Path):
        lists_dir = tmp_path / "downloaded"
        lists_dir.mkdir()
        (lists_dir / "aaa.txt").write_text("https://youtu.be/one\n", encoding="utf-8")
        (lists_dir / "bbb.txt").write_text("https://youtu.be/two\n", encoding="utf-8")

        extra = tmp_path / "local.txt"
        extra.write_text("https://youtu.be/three\n", encoding="utf-8")

        urls = ry.load_all_urls(lists_dir, extra)
        templates = {u.template for u in urls}
        assert templates == {
            "https://youtu.be/one",
            "https://youtu.be/two",
            "https://youtu.be/three",
        }

    def test_lists_dir_only(self, tmp_path: Path):
        lists_dir = tmp_path / "downloaded"
        lists_dir.mkdir()
        (lists_dir / "aaa.txt").write_text("https://youtu.be/one\n", encoding="utf-8")
        urls = ry.load_all_urls(lists_dir, None)
        assert [u.template for u in urls] == ["https://youtu.be/one"]

    def test_urls_file_only_when_lists_dir_absent(self, tmp_path: Path):
        extra = tmp_path / "local.txt"
        extra.write_text("https://youtu.be/three\n", encoding="utf-8")
        urls = ry.load_all_urls(tmp_path / "missing_dir", extra)
        assert [u.template for u in urls] == ["https://youtu.be/three"]

    def test_empty_pool_raises(self, tmp_path: Path):
        # Empty lists dir, no --urls file -> ValueError.
        lists_dir = tmp_path / "downloaded"
        lists_dir.mkdir()
        with pytest.raises(ValueError):
            ry.load_all_urls(lists_dir, None)

    def test_broken_cached_list_is_skipped_not_fatal(self, tmp_path: Path):
        lists_dir = tmp_path / "downloaded"
        lists_dir.mkdir()
        # One empty (invalid) list, one good list -> good one still loads.
        (lists_dir / "empty.txt").write_text("# nothing\n", encoding="utf-8")
        (lists_dir / "good.txt").write_text("https://youtu.be/ok\n", encoding="utf-8")
        urls = ry.load_all_urls(lists_dir, None)
        assert [u.template for u in urls] == ["https://youtu.be/ok"]


# ---------------------------------------------------------------------------
# ChromeController.seek_player_to_live
# ---------------------------------------------------------------------------
#
# seek_player_to_live() belongs to the CDP/browser layer, but unlike the rest
# of ChromeController it never touches the websocket itself -- it only calls
# mw.page_conn.call(...). That lets us unit-test its *contract* by injecting a
# fake connection and asserting on the CDP request it emits, without a live
# browser. These tests are deliberately strict about the in-page JS because the
# whole point of the feature is that it must (a) only ever act on genuine
# livestreams and (b) never disturb %s random-start VODs -- a future refactor
# that loosens the detection should fail here loudly.


class _RecordingConn:
    """Stand-in for CDPConnection that records .call() invocations.

    It performs no I/O. Pass ``raise_exc`` to simulate a CDP/websocket failure
    so we can prove seek_player_to_live() treats such errors as non-fatal.
    """

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raise_exc = raise_exc

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0):
        self.calls.append((method, params or {}))
        if self._raise_exc is not None:
            raise self._raise_exc
        return {}


def _run_seek_to_live(conn: _RecordingConn) -> None:
    """Invoke seek_player_to_live with a fake connection.

    ChromeController.__init__ dials a live browser, so we bypass it with
    object.__new__ -- seek_player_to_live only needs the ManagedWindow's
    page_conn, not any connected state on the controller.
    """
    controller = object.__new__(ry.ChromeController)
    mw = ry.ManagedWindow(
        target_id="t-1",
        window_id=1,
        page_ws_url="ws://127.0.0.1:9222/devtools/page/t-1",
        page_conn=conn,  # type: ignore[arg-type]
    )
    controller.seek_player_to_live(mw)


class TestSeekPlayerToLive:
    def test_emits_single_runtime_evaluate(self):
        conn = _RecordingConn()
        _run_seek_to_live(conn)
        # Exactly one CDP round-trip, and it must be a JS evaluation.
        assert len(conn.calls) == 1
        method, _params = conn.calls[0]
        assert method == "Runtime.evaluate"

    def test_evaluate_uses_by_value_and_no_await(self):
        conn = _RecordingConn()
        _run_seek_to_live(conn)
        _method, params = conn.calls[0]
        # returnByValue keeps the result serialisable; the seek must not block
        # on a promise (there is none to await).
        assert params.get("returnByValue") is True
        assert params.get("awaitPromise") is False
        assert isinstance(params.get("expression"), str)

    def test_detection_is_strictly_infinite_duration(self):
        # Regression guard for the core invariant: live detection keys off the
        # infinite-duration signal, NOT the .ytp-live CSS class (which can
        # linger on past-stream VODs and would wrongly hijack a %s start).
        conn = _RecordingConn()
        _run_seek_to_live(conn)
        expr = conn.calls[0][1]["expression"]
        assert "Infinity" in expr
        assert "duration" in expr
        # The bare ".ytp-live" class must not be used as a detection gate; only
        # the ".ytp-live-badge" button is referenced (for the click-to-live).
        assert "'.ytp-live'" not in expr
        assert '".ytp-live"' not in expr

    def test_seeks_to_live_head_via_badge_and_seekable_fallback(self):
        conn = _RecordingConn()
        _run_seek_to_live(conn)
        expr = conn.calls[0][1]["expression"]
        # Primary path: click the LIVE badge to jump to the live head.
        assert ".ytp-live-badge" in expr
        # Fallback path: hard-seek to the end of the seekable range.
        assert "seekable" in expr
        assert "currentTime" in expr

    def test_cdp_failure_is_non_fatal(self):
        # A failing CDP call must never bubble up and break the rotation loop.
        conn = _RecordingConn(raise_exc=RuntimeError("ws boom"))
        _run_seek_to_live(conn)  # should not raise
        # The attempt was still made before the error was swallowed.
        assert len(conn.calls) == 1

    def test_timeout_error_is_also_swallowed(self):
        # Any exception type from the CDP layer (not just RuntimeError) is
        # non-fatal -- the loop keeps rotating regardless.
        conn = _RecordingConn(raise_exc=TimeoutError("no response"))
        _run_seek_to_live(conn)  # should not raise
        assert len(conn.calls) == 1

