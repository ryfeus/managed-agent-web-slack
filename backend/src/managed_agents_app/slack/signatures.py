from __future__ import annotations

import hashlib

from slack_sdk.signature import SignatureVerifier


def verify_slack_signature(
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    raw_body: str,
) -> bool:
    if not timestamp or not signature:
        return False
    return bool(
        SignatureVerifier(signing_secret=signing_secret).is_valid(
            body=raw_body,
            timestamp=timestamp,
            signature=signature,
        )
    )


def interaction_id(raw_body: str) -> str:
    return hashlib.sha256(raw_body.encode()).hexdigest()
