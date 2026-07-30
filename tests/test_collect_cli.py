"""The command line and the atomic write.

Collection itself needs a network, so what is tested here is argument handling,
the summary path, and the write that must not be able to truncate a good file.
"""

from __future__ import annotations

import json

import pytest

from collector.collect import build_parser, main, write_atomic
from collector.constants import LOCAL_PATH, STATS_PATH


class TestArguments:
    def test_the_default_horizon_is_a_year_of_dates(self):
        args = build_parser().parse_args([])
        assert args.horizon == 365

    def test_a_window_can_be_given_explicitly(self):
        args = build_parser().parse_args(["--since", "2025-07-30",
                                         "--until", "2026-07-29"])
        assert args.since.isoformat() == "2025-07-30"
        assert args.until.isoformat() == "2026-07-29"

    def test_half_a_window_is_refused(self, capsys):
        """One end without the other is a mistake rather than a shorthand."""
        with pytest.raises(SystemExit):
            main(["--since", "2025-07-30"])
        assert "together" in capsys.readouterr().err

    def test_verbose_is_off_by_default(self):
        """Workflow logs on a public repository are public."""
        assert build_parser().parse_args([]).verbose is False

    def test_an_invalid_date_is_refused(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--since", "not-a-date",
                                       "--until", "2026-07-29"])


class TestSummary:
    def test_it_prints_from_a_file_without_collecting(self, tmp_path, payload, capsys):
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(payload))
        assert main(["--summary", str(path)]) == 0
        out = capsys.readouterr().out
        assert "derived figures" in out
        assert "reviews given" in out

    def test_it_needs_no_token(self, tmp_path, payload, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(payload))
        assert main(["--summary", str(path)]) == 0
        capsys.readouterr()

    def test_the_active_date_count_is_derived_from_the_records(self, tmp_path,
                                                               payload, capsys):
        """Nothing in the file states how many dates saw activity, so the summary
        can only report a figure it worked out from the records themselves."""
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(payload))
        main(["--summary", str(path)])
        line = next(line for line in capsys.readouterr().out.splitlines()
                    if "active dates" in line)
        assert line.split()[-1] == "5"

    def test_the_time_to_merge_is_derivable_from_the_published_file(
            self, tmp_path, payload, capsys):
        """Recording only dates would read as no time at all for every pull
        request merged the day it was opened."""
        from collector.assemble import strip_attribution
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(strip_attribution(payload)))
        main(["--summary", str(path)])
        assert "time to merge" in capsys.readouterr().out


class TestMissingToken:
    def test_collection_fails_clearly_without_one(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        assert main(["--horizon", "30"]) == 1
        assert "token" in capsys.readouterr().err

    def test_an_impossible_window_fails_before_any_request(self, monkeypatch, capsys):
        """Failing here beats failing partway through a four-minute run."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        assert main(["--since", "2025-07-29", "--until", "2026-07-29"]) == 1
        assert "366 dates" in capsys.readouterr().err


class TestAtomicWrite:
    def test_it_replaces_the_target_and_leaves_no_temporary(self, tmp_path):
        target = tmp_path / "stats.json"
        target.write_text('{"old": true}')
        write_atomic(target, {"new": True})
        assert json.loads(target.read_text()) == {"new": True}
        assert not list(tmp_path.glob("*.tmp"))

    def test_it_creates_a_missing_directory(self, tmp_path):
        target = tmp_path / "data" / "stats.json"
        write_atomic(target, {"ok": True})
        assert json.loads(target.read_text()) == {"ok": True}

    def test_a_failure_mid_write_leaves_the_previous_file_intact(self, tmp_path,
                                                                 monkeypatch):
        """This is the whole reason for writing through a temporary file. Writing
        in place would truncate a good file the moment a run died."""
        target = tmp_path / "stats.json"
        target.write_text('{"good": true}')

        from pathlib import Path as RealPath
        original_replace = RealPath.replace

        def fail_on_rename(self, other):
            raise OSError("interrupted")

        monkeypatch.setattr(RealPath, "replace", fail_on_rename)
        with pytest.raises(OSError):
            write_atomic(target, {"replacement": True})
        monkeypatch.setattr(RealPath, "replace", original_replace)

        assert json.loads(target.read_text()) == {"good": True}

    def test_output_does_not_depend_on_key_order(self, tmp_path):
        """Two runs producing the same data must produce the same bytes."""
        target = tmp_path / "stats.json"
        write_atomic(target, {"b": 1, "a": 2})
        first = target.read_text()
        write_atomic(target, {"a": 2, "b": 1})
        assert target.read_text() == first


class TestPaths:
    def test_both_files_live_under_a_data_directory(self):
        assert STATS_PATH.parent.name == "data"
        assert LOCAL_PATH.parent == STATS_PATH.parent

    def test_the_attributed_file_is_named_to_look_local(self):
        assert "local" in LOCAL_PATH.name
