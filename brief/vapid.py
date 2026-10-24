"""Web Push (VAPID) keys: generate them, and resolve the pair from the
environment so a mistyped, swapped or mismatched public key can't break
notifications - the public key is always derived from the private one."""

from __future__ import annotations

import base64
import os
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .config import clean_secret

_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # P-256 group order


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def is_private(value: str) -> bool:
    try:
        raw = _decode(value)
    except ValueError:
        return False
    return len(raw) == 32 and 0 < int.from_bytes(raw, "big") < _ORDER


def is_public(value: str) -> bool:
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _decode(value))
        return True
    except ValueError:
        return False


def public_from_private(private: str) -> str:
    key = ec.derive_private_key(int.from_bytes(_decode(private), "big"), ec.SECP256R1())
    return _encode(key.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint))


_MAILTO = re.compile(r"mailto:[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", re.I)
_HTTPS = re.compile(r"https://[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", re.I)
_PLACEHOLDER = re.compile(r"(@(example\.(com|org|net)|localhost|yourdomain\.com)$)|^mailto:(you|me)@gmail\.com$", re.I)


def subject(env=os.environ) -> str:
    """The contact sent with every push (VAPID "sub"). Apple answers BadJwtToken unless it is a
    clean mailto: or https: URL, so tidy up common paste mistakes and fall back to the site's
    own address when it's missing, a placeholder, or still malformed."""
    raw = "".join(env.get("VAPID_SUBJECT", "").split()).replace("<", "").replace(">", "")
    if "@" in raw and not raw.lower().startswith("mailto:"):
        raw = "mailto:" + raw
    if (_MAILTO.fullmatch(raw) and not _PLACEHOLDER.search(raw)) or _HTTPS.fullmatch(raw):
        return raw
    owner = env.get("GITHUB_REPOSITORY_OWNER", "")
    return f"https://{owner.lower()}.github.io" if owner else "https://github.com"


def generate() -> tuple[str, str]:
    """(private, public), base64url, the format browsers and pywebpush expect."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = _encode(key.private_numbers().private_value.to_bytes(32, "big"))
    return private, public_from_private(private)


def resolve(env=os.environ) -> tuple[str, str, list[str]]:
    """(private, public, problems) from VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY."""
    private = clean_secret(env.get("VAPID_PRIVATE_KEY", ""))
    public = clean_secret(env.get("VAPID_PUBLIC_KEY", ""))
    problems = []
    if is_public(private) and is_private(public):
        private, public = public, private
        problems.append("VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY are swapped; using them the right way round")
    if private:
        if not is_private(private):
            problems.append(f"VAPID_PRIVATE_KEY isn't a valid private key: it should be 43 characters, "
                            f"and it has {len(private)}. Generate a new pair (README step 3)")
            return "", public if is_public(public) else "", problems
        derived = public_from_private(private)
        if public and public != derived:
            problems.append("VAPID_PUBLIC_KEY doesn't match VAPID_PRIVATE_KEY; using the public key "
                            "calculated from the private key")
        return private, derived, problems
    if public and not is_public(public):
        problems.append(f"VAPID_PUBLIC_KEY isn't a valid public key: it should be 87 characters starting "
                        f"with 'B', and it has {len(public)}")
        public = ""
    return private, public, problems
