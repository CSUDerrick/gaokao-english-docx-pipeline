#!/usr/bin/env python3
"""Make Python trust the certificates macOS already trusts.

A teacher installed the app on a new Mac and could not get past the API test:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    self-signed certificate in certificate chain

That message does not mean her key was wrong, or that DeepSeek was down. It means
*something on that Mac is intercepting TLS* — a school or company proxy, antivirus
(360 / 火绒 / Kaspersky and friends), or a VPN. Those products install their own root
certificate into the **macOS Keychain** and re-sign every HTTPS connection with it.
Safari and Chrome are fine, because they ask macOS what to trust. Python is not: it
carries its own CA bundle and never looks at the Keychain, so the re-signed chain
terminates in a root it has never heard of.

``truststore`` fixes exactly this by routing verification through macOS's own Security
framework — the interception root is in there, so the chain validates. Certifi does not
help here (the corporate root is not in Mozilla's bundle), which is why it is only the
fallback: it covers the *other* failure, a build that shipped no CA bundle at all.

When even that fails, the root was never installed in the Keychain and no amount of
configuration will verify the chain. ``insecure`` exists for that case only — it is the
same escape hatch as ``curl -k`` and ``pip --trusted-host``, and it is off by default.
"""

from __future__ import annotations

import ssl

# Captured before any injection, so the insecure context can always be built from the
# real standard library rather than from whatever replaced it.
_STDLIB_CREATE_DEFAULT_CONTEXT = ssl.create_default_context
# truststore replaces ssl.SSLContext itself, and its class verifies the *peer* — which a
# server socket has none of. Anything that needs a genuine server-side context (the TLS
# tests) has to build it from this, or it breaks as soon as anything in the process has
# called install().
STDLIB_SSL_CONTEXT = ssl.SSLContext

SYSTEM = "system"    # macOS Keychain — sees corporate/AV roots
CERTIFI = "certifi"  # Mozilla's bundle — does not
FALLBACK = "python"  # whatever Python was built with

_mode: str | None = None


def install() -> str:
    """Point Python's TLS verification at the OS trust store. Safe to call twice.

    Must run before anything else creates an SSLContext, so it goes at the very top of
    both entry points (the app and the CLI).
    """
    global _mode
    if _mode is not None:
        return _mode

    try:
        import truststore

        truststore.inject_into_ssl()
        _mode = SYSTEM
        return _mode
    except Exception:  # noqa: BLE001 — never let a trust-store tweak stop the program
        pass

    try:
        import os

        import certifi

        # Only fills in a bundle if the build shipped without one; it cannot see a
        # corporate root, so it does not solve the interception case.
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        _mode = CERTIFI
        return _mode
    except Exception:  # noqa: BLE001
        _mode = FALLBACK
        return _mode


def mode() -> str:
    return _mode or FALLBACK


def context(insecure: bool = False) -> ssl.SSLContext:
    if not insecure:
        return ssl.create_default_context()

    # Built from the pre-injection stdlib factory: asking the injected truststore
    # context to stop verifying is not what it is for.
    ctx = _STDLIB_CREATE_DEFAULT_CONTEXT()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_certificate_error(error: BaseException) -> bool:
    """Is this the interception failure, rather than a wrong key or a dead network?

    Worth separating: the two need completely different things from the teacher, and
    the raw message ("CERTIFICATE_VERIFY_FAILED ... self-signed certificate in
    certificate chain") tells her nothing she can act on.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        text = str(error)
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
            return True
        error = getattr(error, "reason", None) or error.__cause__
    return False


CERTIFICATE_HELP = (
    "HTTPS 证书校验失败：你的网络里有东西在中间拦截加密连接"
    "（学校/公司代理、杀毒软件或 VPN）。这不是密钥的问题。\n"
    "· 通常重启本程序即可——它会改用 macOS 钥匙串里的证书。\n"
    "· 如果还不行，去「调试模式」勾上「跳过 HTTPS 证书校验」再试一次。"
)
