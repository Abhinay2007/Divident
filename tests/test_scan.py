import json

from dividend.cli import main
from dividend.scan import build_report, group_by_source, parse_rows


def test_dedupes_binaries_into_source():
    rows = parse_rows(
        "ii \tlo-core\tlo\n"
        "ii \tlo-writer\tlo\n"
        "ii \tbash\tbash\n"
    )

    assert group_by_source(rows) == {
        "bash": ["bash"],
        "lo": ["lo-core", "lo-writer"],
    }


def test_missing_source_falls_back_to_binary():
    assert parse_rows("ii \tfoo\t\n") == [("foo", "foo")]


def test_skips_removed_and_malformed_rows():
    rows = parse_rows(
        "rc \told\told\n"
        "garbage\n"
        "ii \tfoo\n"
        "ii \tbar\tbar\n"
    )

    assert rows == [("bar", "bar")]


def test_report_is_order_independent():
    a = build_report(
        parse_rows("ii \tb\ts\n" "ii \ta\ts\n"),
        "demo",
    )
    b = build_report(
        parse_rows("ii \ta\ts\n" "ii \tb\ts\n"),
        "demo",
    )

    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_demo_cli_writes_json(tmp_path):
    output_file = tmp_path / "scan.json"

    assert main(["scan", "--demo", "-o", str(output_file)]) == 0

    report = json.loads(output_file.read_text())
    assert report["source_count"] > 0
