"""Regression tests for two ways a healthy AudioVault download was being
thrown away:

  - a connection dropped mid-body raised ChunkedEncodingError, which
    `_is_transient` classified as terminal, so the episode was dropped
    instead of re-queued (live 2026-09-04, Gossip Girl S02E06);
  - a correct MP3 body served under a bogus Content-Type was rejected on the
    header alone, stranding the title on every nightly retry (live since
    2026-08-29, Borat).
"""
import types

import pytest
import requests

from describarr import audiovault as av
from describarr import server


# --- transient classification ---------------------------------------------

def _http_error(status):
    exc = requests.HTTPError("boom")
    exc.response = types.SimpleNamespace(status_code=status)
    return exc


@pytest.mark.parametrize("exc", [
    requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),
    requests.ConnectionError("reset"),
    requests.Timeout("slow"),
    requests.exceptions.ContentDecodingError("bad gzip"),
    ConnectionResetError("peer"),
    TimeoutError("slow"),
    _http_error(503),
    _http_error(429),
])
def test_transient_errors_requeue(exc):
    assert server._is_transient(exc) is True


@pytest.mark.parametrize("exc", [
    _http_error(404),
    _http_error(401),
    RuntimeError("unexpected content-type"),
    av.LoginError("still redirecting to /login"),
    ValueError("programming bug"),
])
def test_terminal_errors_drop(exc):
    assert server._is_transient(exc) is False


def test_chunked_encoding_error_is_not_a_connection_error():
    """The reason the enumerated tuple missed it — guards against a future
    'tidy-up' that narrows _TRANSIENT_WORKER_ERRORS back to the connection
    classes."""
    assert not issubclass(requests.exceptions.ChunkedEncodingError, requests.ConnectionError)
    assert issubclass(requests.exceptions.ChunkedEncodingError, requests.RequestException)


# --- body sniffing when Content-Type lies ----------------------------------

class _FakeResponse:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, body, content_type, disposition="attachment; filename=x.mp3"):
        self._body = body
        self.headers = {"Content-Type": content_type, "Content-Disposition": disposition}
        self.url = "https://audiovault.net/download/3462"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65_536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def _client_returning(resp):
    client = av.AudioVaultClient.__new__(av.AudioVaultClient)
    client._get_with_relogin = lambda url, **kw: resp
    return client


# Real bytes off the head of the Borat download AudioVault mislabels.
_ID3_MP3 = b"ID3\x03\x00\x00\x00\x00\x05sTYER\x00\x00\x00\x0b" + b"\x00" * 4096


def test_mislabelled_mp3_body_is_downloaded(tmp_path):
    resp = _FakeResponse(_ID3_MP3, "application/x-font-gdos")
    dest = _client_returning(resp).download("https://audiovault.net/download/3462", tmp_path)
    assert dest.read_bytes() == _ID3_MP3, "sniffed head must be written, not dropped"


def test_login_page_body_is_still_rejected(tmp_path):
    resp = _FakeResponse(b"<!DOCTYPE html><html><body>Sign in</body></html>", "text/html")
    with pytest.raises(RuntimeError, match="unexpected content-type"):
        _client_returning(resp).download("https://audiovault.net/download/1", tmp_path)
    assert list(tmp_path.iterdir()) == [], "nothing may be left behind on reject"


def test_acceptable_content_type_skips_the_sniff(tmp_path):
    body = b"PK\x03\x04" + b"\x00" * 128
    resp = _FakeResponse(body, "application/zip", disposition="attachment; filename=s2.zip")
    dest = _client_returning(resp).download("https://audiovault.net/download/2", tmp_path)
    assert dest.name == "s2.zip"
    assert dest.read_bytes() == body


@pytest.mark.parametrize("head,expected", [
    (b"ID3\x03\x00", True),
    (b"\xff\xfb\x90\x00", True),
    (b"PK\x03\x04", True),
    (b"\x00\x00\x00 ftypM4A ", True),
    (b"<!DOCTYPE html>", False),
    (b"  <html>", False),
    (b"", False),
    (b"not media at all", False),
])
def test_looks_like_media(head, expected):
    assert av._looks_like_media(head) is expected
