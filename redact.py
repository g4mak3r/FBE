"""Small redaction helper for remote error bodies; never include credentials."""
import re

JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def redact(value, *secrets):
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return JWT.sub("[REDACTED JWT]", text)
