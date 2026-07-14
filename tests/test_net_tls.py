"""HTTPS on a Mac whose network re-signs certificates.

A teacher installed the app on a new Mac and could not get past the API test:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    self-signed certificate in certificate chain

Something on that machine intercepts TLS (school proxy, antivirus, VPN) and re-signs
every connection with its own root. macOS trusts that root — it is in the Keychain —
but Python carries its own CA bundle and never looks there, so the chain terminates in
an issuer it has never heard of.

The interception cannot be reproduced on this machine, so these tests reproduce its
*shape* instead: a real HTTPS server holding a certificate signed by a root Python does
not know about. That is the same failure, and it lets us prove the fix rather than
assert it.
"""

from __future__ import annotations

import http.server
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import net_tls  # noqa: E402


def _self_signed(tmp: Path) -> tuple[Path, Path]:
    """A certificate and key for localhost, signed by nobody Python trusts."""
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost",
        ],
        check=True, capture_output=True,
    )
    return cert, key


class _Server:
    def __init__(self, cert: Path, key: Path):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):  # keep the test output clean
        pass


def _get(port: int, context: ssl.SSLContext) -> str:
    with urllib.request.urlopen(f"https://localhost:{port}/", context=context, timeout=10) as response:
        return response.read().decode()


def test_an_untrusted_root_is_rejected_by_default():
    # The baseline. Without this failing, the next test proves nothing.
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = _self_signed(Path(tmp))
        with _Server(cert, key) as server:
            try:
                _get(server.port, net_tls.context())
            except Exception as exc:  # noqa: BLE001
                assert net_tls.is_certificate_error(exc), f"expected a certificate failure, got {exc!r}"
            else:
                raise AssertionError("an unknown root must not verify")


def test_verification_is_delegated_to_the_macos_keychain():
    """The actual fix, asserted directly.

    A trusted root cannot be conjured up inside a test — putting one in the Keychain
    needs an admin prompt. But the property that matters *is* checkable: after install(),
    the context Python verifies with is truststore's, which consults macOS's Security
    framework rather than a bundle compiled into Python. That is precisely why the
    teacher's interception root (which macOS already trusts) starts working.

    Note that truststore deliberately ignores ``load_verify_locations`` — the OS store is
    the only source of truth. So the pass/fail of this test cannot be faked by loading a
    CA file, which is what makes it worth having.
    """
    net_tls.install()
    context = net_tls.context()
    assert type(context).__module__.startswith("truststore"), (
        f"verification is going through {type(context).__module__}, not the OS trust store — "
        "certifi cannot see a corporate/AV root and the teacher's Mac would still fail"
    )


def test_a_root_the_os_does_not_know_is_still_rejected():
    # The system store is not a blank cheque: an unknown issuer must still fail, or the
    # "fix" would just be verification turned off with extra steps.
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = _self_signed(Path(tmp))
        with _Server(cert, key) as server:
            try:
                _get(server.port, net_tls.context())
            except Exception as exc:  # noqa: BLE001
                assert net_tls.is_certificate_error(exc)
            else:
                raise AssertionError("an unknown root must not verify just because truststore is on")


def test_insecure_skips_verification_entirely():
    # The last resort, for a network whose root was never put in the Keychain at all.
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = _self_signed(Path(tmp))
        with _Server(cert, key) as server:
            assert _get(server.port, net_tls.context(insecure=True)) == "ok"


def test_insecure_is_never_the_default():
    assert net_tls.context().verify_mode == ssl.CERT_REQUIRED
    assert net_tls.context().check_hostname is True
    assert net_tls.context(insecure=True).verify_mode == ssl.CERT_NONE


def test_a_certificate_failure_is_told_apart_from_a_bad_key():
    # They need completely different things from the teacher, and the raw OpenSSL
    # message tells her nothing she can act on.
    verify = ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")
    assert net_tls.is_certificate_error(verify)
    assert net_tls.is_certificate_error(OSError("[SSL: CERTIFICATE_VERIFY_FAILED] ..."))
    assert not net_tls.is_certificate_error(RuntimeError("HTTP 401 Unauthorized"))
    assert not net_tls.is_certificate_error(TimeoutError("timed out"))


def test_install_reports_which_trust_store_it_reached():
    mode = net_tls.install()
    assert mode in (net_tls.SYSTEM, net_tls.CERTIFI, net_tls.FALLBACK)
    assert net_tls.install() == mode, "idempotent — the entry points both call it"
    # On the machines this ships to, the OS store is what makes the interception case
    # work; certifi cannot see a corporate root.
    assert mode == net_tls.SYSTEM, "truststore should be installed; certifi alone would not fix the teacher's Mac"


# --- proxies. Moving the client off urllib.request.urlopen (for cancellation and for the
# --- SSL context) silently dropped proxy support: urlopen reads the proxy out of the
# --- environment and out of macOS System Settings, http.client does not. That is not an
# --- edge case — a school network that re-signs TLS is usually re-signing it *at a
# --- proxy*, so losing it would have broken the very machine this release exists to fix.


def _connection(url: str, monkey_proxies: dict):
    import http.client
    import urllib.parse
    import urllib.request

    import gaokao_english_docx_pipeline as pipeline

    real = urllib.request.getproxies
    urllib.request.getproxies = lambda: monkey_proxies
    try:
        parts = urllib.parse.urlsplit(url)
        conn = pipeline._open_connection(parts, timeout=5, insecure_ssl=False)
        pipeline._close_connection(conn)
        return conn
    finally:
        urllib.request.getproxies = real


def test_a_system_proxy_is_used_and_tunnelled_through():
    conn = _connection("https://api.deepseek.com/chat/completions",
                       {"https": "http://127.0.0.1:1082"})
    assert (conn.host, conn.port) == ("127.0.0.1", 1082), "the socket must go to the proxy"
    assert conn._tunnel_host == "api.deepseek.com", "and CONNECT through it to the real host"
    assert conn._tunnel_port == 443


def test_with_no_proxy_it_connects_straight_to_the_api():
    conn = _connection("https://api.deepseek.com/chat/completions", {})
    assert (conn.host, conn.port) == ("api.deepseek.com", 443)
    assert conn._tunnel_host is None


def test_a_loopback_address_never_goes_out_through_the_proxy():
    # urllib.request.proxy_bypass says *no* for 127.0.0.1 on macOS — the bypass list only
    # holds what the user typed, and nobody types localhost. Sending a loopback address to
    # a proxy is never what anyone means (and it would break the cancellation tests, which
    # talk to a server on 127.0.0.1).
    conn = _connection("http://127.0.0.1:9999/chat/completions",
                       {"http": "http://127.0.0.1:1082"})
    assert (conn.host, conn.port) == ("127.0.0.1", 9999)
    assert conn._tunnel_host is None
