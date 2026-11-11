import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from . import scan
from .db import create_database_engine, create_session_factory
from .demo import configure_sandbox_recipients

DEMO_FIXTURE = Path(__file__).parent / "data" / "demo_dpkg.tsv"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dividend")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="scan installed packages")
    scan_parser.add_argument("--demo", action="store_true", help="use bundled sample data")
    scan_parser.add_argument("-o", "--output", default="-", help="JSON output path, or - for stdout")
    recipients_parser = sub.add_parser(
        "configure-sandbox-recipients",
        help="configure opted-in demo maintainers for PayPal Sandbox",
    )
    recipients_parser.add_argument("--database-url", required=True)
    recipients_parser.add_argument("--file", required=True, dest="recipients_file")
    args = parser.parse_args(argv)
    if args.command == "configure-sandbox-recipients":
        engine = None
        try:
            url = make_url(args.database_url)
            if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
                raise ValueError("an existing file-backed SQLite database is required")
            database_path = Path(url.database)
            if not database_path.is_file():
                raise ValueError("database file does not exist")
            engine = create_database_engine(args.database_url)
            count = configure_sandbox_recipients(
                create_session_factory(engine), args.recipients_file,
            )
        except Exception:
            print("Failed to configure sandbox recipients.", file=sys.stderr)
            return 2
        finally:
            if engine is not None:
                engine.dispose()
        print(count)
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
