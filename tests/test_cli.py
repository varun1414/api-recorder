from __future__ import annotations

import json

import pytest
from factories import capture, interaction

from apirecorder.cli import main


@pytest.fixture
def baseline(tmp_path):
    directory = tmp_path / "baseline"
    cap = capture(
        "subscription_lifecycle",
        interaction("POST", "/v1/subs", 201, {"id": "sub_1", "period_end": 99}),
        interaction("GET", "/v1/subs/sub_1", 200, {"id": "sub_1", "period_end": 99}),
        interaction("GET", "/v1/subs", 200, {"data": [], "has_more": True}),
    )
    cap.write(directory / "subscription_lifecycle.json")
    return directory


@pytest.fixture
def drifted(tmp_path):
    directory = tmp_path / "current"
    cap = capture(
        "subscription_lifecycle",
        interaction("POST", "/v1/subs", 202, {"id": "sub_2"}),
        interaction("GET", "/v1/subs/sub_2", 200, {"id": "sub_2", "period_end": 99}),
        interaction("GET", "/v1/subs", 200, {"data": [], "next_cursor": "c1"}),
    )
    cap.write(directory / "subscription_lifecycle.json")
    return directory


def test_inspect_lists_the_calls(baseline, capsys):
    assert main(["inspect", str(baseline)]) == 0
    out = capsys.readouterr().out
    assert "subscription_lifecycle - 3 interaction(s)" in out
    assert "/v1/subs/{id}" in out
    assert "201" in out


def test_report_to_stdout_separates_observed_from_gaps(baseline, capsys):
    assert main(["report", str(baseline)]) == 0
    out = capsys.readouterr().out
    assert "## Observed" in out
    assert "## Not observed" in out
    assert "`period_end`" in out
    assert "final page was never fetched" in out


def test_report_to_file(baseline, tmp_path, capsys):
    target = tmp_path / "docs" / "behaviour.md"
    assert main(["report", str(baseline), "-o", str(target)]) == 0
    assert "## Observed" in target.read_text(encoding="utf-8")


def test_diff_exits_1_on_breaking_change(baseline, drifted, capsys):
    assert main(["diff", str(baseline), str(drifted)]) == 1
    out = capsys.readouterr().out
    assert "BREAKING" in out
    assert "primary status 201 -> 202" in out
    assert "`has_more` (boolean) is gone" in out
    assert "`next_cursor` (string) added" in out


def test_diff_exits_0_when_nothing_changed(baseline, capsys):
    assert main(["diff", str(baseline), str(baseline)]) == 0
    assert "No changes against baseline" in capsys.readouterr().out


def test_diff_markdown_format(baseline, drifted, tmp_path):
    target = tmp_path / "drift.md"
    assert main(["diff", str(baseline), str(drifted), "--format", "markdown", "-o", str(target)]) == 1
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# API drift")
    assert "## Breaking" in text


def test_diff_no_info_hides_info_lines(baseline, drifted, capsys):
    main(["diff", str(baseline), str(drifted), "--no-info"])
    assert "INFO" not in capsys.readouterr().out


def test_strict_fails_on_additive_only(tmp_path, capsys):
    base_dir, curr_dir = tmp_path / "b", tmp_path / "c"
    capture("f", interaction("GET", "/u", 200, {"id": 1})).write(base_dir / "f.json")
    capture("f", interaction("GET", "/u", 200, {"id": 1, "extra": 2})).write(
        curr_dir / "f.json"
    )

    assert main(["diff", str(base_dir), str(curr_dir)]) == 0
    assert main(["diff", str(base_dir), str(curr_dir), "--strict"]) == 1


def test_missing_recordings_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        main(["inspect", str(empty)])


def test_non_capture_json_is_skipped(tmp_path, capsys):
    directory = tmp_path / "mixed"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"unrelated": True}))
    capture("f", interaction("GET", "/u", 200, {"id": 1})).write(directory / "f.json")

    assert main(["inspect", str(directory)]) == 0
    assert "f - 1 interaction(s)" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage: apirecorder" in capsys.readouterr().out
