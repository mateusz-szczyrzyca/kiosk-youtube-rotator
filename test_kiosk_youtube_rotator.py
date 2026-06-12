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
    """

    def __init__(self, content: bytes = b"", status: int = 200):
        self.content = content
        self.status_code = status

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

