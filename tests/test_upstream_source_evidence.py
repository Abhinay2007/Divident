import httpx

from dividend.upstream.candidates import RepositoryCandidate
from dividend.upstream.source_evidence import collect_candidate_evidence
from dividend.upstream.verifier import TRUSTED_PROJECT_SOURCES, TRUST_POLICY_ID


def candidate(source_name="curl", url="https://github.com/curl/curl"):
    return RepositoryCandidate(source_name, url, "curl", "curl", "github_repository_search", (),
                              None, None, None, None)


class Response:
    def __init__(self, status=200, body="", headers=None, encoding="utf-8"):
        self.status_code = status
        self.content = body.encode(encoding)
        self.headers = headers or {}
        self.encoding = encoding

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def iter_bytes(self):
        yield self.content


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_exact_repository_link_produces_immutable_provenance_evidence():
    repo = "https://github.com/curl/curl"
    client = Client(Response(body=f'<html><a href="{repo}">source</a>'
                                   f'<a href="{repo}/issues">issues</a></html>'))
    result = collect_candidate_evidence(client, candidate())
    assert result.status == "candidate_link_found" and result.page_loaded
    assert client.calls[0][1] == TRUSTED_PROJECT_SOURCES["curl"]
    assert client.calls[0][2]["timeout"] == 5.0
    assert result.evidence[0].observed_href == repo
    assert result.evidence[0].observed_repository_url == repo
    assert result.evidence[0].normalized_repository_key == "github.com/curl/curl"
    assert len(result.evidence) == 1
    assert result.evidence[0].source_trust_policy_id == TRUST_POLICY_ID
    assert result.evidence[0].http_status == 200
    assert result.evidence[0].retrieved_at.tzinfo is not None
    try:
        result.evidence[0].http_status = 201
    except AttributeError:
        pass
    else:
        raise AssertionError("evidence must be immutable")


def test_successful_page_without_repository_link_is_not_identity_verification():
    result = collect_candidate_evidence(Client(Response(body="<html><a href='/news/'>News</a></html>")), candidate())
    assert result.page_loaded and result.status == "page_loaded_unverified"
    assert result.http_status == 200 and not result.candidate_link_found and result.evidence == ()


def test_wrong_repository_link_is_recorded_but_does_not_match_candidate():
    wrong = "https://github.com/someone/curl"
    result = collect_candidate_evidence(Client(Response(body=f'<a href="{wrong}">source</a>')), candidate())
    assert result.status == "page_loaded_unverified" and not result.candidate_link_found
    assert result.evidence[0].observed_repository_url == wrong
    assert result.evidence[0].normalized_repository_key == "github.com/someone/curl"


def test_github_repository_subpages_are_not_mistaken_for_repository_links():
    body = '<a href="https://github.com/curl/curl/issues">issues</a>'
    result = collect_candidate_evidence(Client(Response(body=body)), candidate())
    assert result.page_loaded and result.status == "page_loaded_unverified"
    assert result.evidence == ()


def test_same_host_redirect_is_followed_and_original_source_provenance_is_kept():
    repo = "https://github.com/curl/curl"
    client = Client(Response(302, headers={"location": "/current/"}), Response(body=f'<a href="{repo}">repo</a>'))
    result = collect_candidate_evidence(client, candidate())
    assert result.status == "candidate_link_found"
    assert [call[1] for call in client.calls] == [TRUSTED_PROJECT_SOURCES["curl"], "https://curl.se/current/"]
    assert result.source_url == TRUSTED_PROJECT_SOURCES["curl"]
    assert result.final_source_url == "https://curl.se/current/"


def test_external_redirect_is_rejected_without_fetching_destination():
    client = Client(Response(302, headers={"location": "https://evil.example/"}))
    result = collect_candidate_evidence(client, candidate())
    assert result.status == "untrusted_source" and not result.page_loaded
    assert len(client.calls) == 1


def test_http_error_is_reported_as_failed_page_fetch():
    result = collect_candidate_evidence(Client(Response(503, "unavailable")), candidate())
    assert result.status == "http_error" and not result.page_loaded
    assert result.http_status == 503 and result.evidence == ()


def test_untrusted_package_source_is_rejected_before_network_call():
    client = Client()
    result = collect_candidate_evidence(client, candidate("openssl", "https://github.com/openssl/openssl"))
    assert result.status == "untrusted_source" and not client.calls


def test_malicious_and_malformed_links_are_ignored():
    body = ('<a href="javascript:alert(1)">script</a>'
            '<a href="https://attacker@github.com/curl/curl">credentials</a>'
            '<a href="https://[::1">malformed</a>')
    result = collect_candidate_evidence(Client(Response(body=body)), candidate())
    assert result.page_loaded and result.status == "page_loaded_unverified"
    assert not result.evidence


def test_timeout_is_reported_without_exposing_exception_details():
    result = collect_candidate_evidence(Client(httpx.ReadTimeout("private request details")), candidate())
    assert result.status == "timeout" and not result.page_loaded
    assert result.evidence == ()


def test_oversized_page_is_stopped_at_the_body_limit():
    class OversizedResponse(Response):
        def iter_bytes(self):
            yield b"x" * 1_000_001
            raise AssertionError("collector must stop reading when the size cap is exceeded")

    result = collect_candidate_evidence(Client(OversizedResponse()), candidate())
    assert result.status == "page_too_large" and not result.page_loaded
