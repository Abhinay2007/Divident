import argparse
import json
import sys
from pathlib import Path

from . import scan

DEMO_FIXTURE = Path(__file__).parent / "data" / "demo_dpkg.tsv"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dividend")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="scan installed packages")
    scan_parser.add_argument("--demo", action="store_true", help="use bundled sample data")
    scan_parser.add_argument("-o", "--output", default="-", help="JSON output path, or - for stdout")
    args = parser.parse_args(argv)
    if args.demo:
        text, origin = DEMO_FIXTURE.read_text(), "demo"
    else:
        text, origin = scan.run_dpkg_query(), "dpkg"
    output = json.dumps(scan.build_report(scan.parse_rows(text), origin), indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(output)
    else:
        Path(args.output).write_text(output)
    return 0
