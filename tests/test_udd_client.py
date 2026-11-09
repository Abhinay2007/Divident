from dividend.enrichment.udd_client import UDDClient


def test_fetch_matches_byte_valued_rows_after_setting_utf8():
    class Cursor:
        def __init__(self):
            self.query = None

        def __enter__(self): return self
        def __exit__(self, *_): pass

        def execute(self, query, params=None):
            if query.startswith("SET LOCAL"):
                assert query == "SET LOCAL client_encoding TO 'UTF8'"
                self.utf8 = True
            else:
                assert self.utf8
                assert params == (["bash"],)
                self.query = query

        def fetchall(self):
            # SQL_ASCII can still expose bytes despite the requested client encoding.
            return [(b"bash", b"https://example.org", b"https://github.com/acme/bash", b"team@example.org")]

    class Connection:
        def __init__(self): self.cursor_instance = Cursor()
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def cursor(self): return self.cursor_instance

    connection = Connection()
    facts = UDDClient("dsn", lambda dsn: connection).fetch(["bash"])

    assert facts["bash"].source_name == "bash"
    assert facts["bash"].repository_url == "https://github.com/acme/bash"
    assert facts["bash"].homepage == "https://example.org"
    assert facts["bash"].contact == "team@example.org"
    assert not facts["bash"].partial


def test_fetch_preserves_string_valued_rows():
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, query, params=None):
            if not query.startswith("SET LOCAL"):
                assert params == (["curl"],)
        def fetchall(self):
            return [("curl", "https://curl.se", "https://github.com/curl/curl", None)]

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def cursor(self): return Cursor()

    facts = UDDClient("dsn", lambda dsn: Connection()).fetch(["curl"])

    assert facts["curl"].source_name == "curl"
    assert facts["curl"].repository_url == "https://github.com/curl/curl"
    assert facts["curl"].homepage == "https://curl.se"
    assert facts["curl"].contact is None
    assert not facts["curl"].partial
