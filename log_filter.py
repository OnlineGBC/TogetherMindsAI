"""
log_filter.py
-------------
Logging filter that redacts sensitive field values before any log record
is emitted (HIPAA § 164.312(b) — audit controls / log hygiene).

Targets JSON-style key/value pairs whose keys are known to carry PHI or
session content:  "text", "content", "message", "body", "prompt", "response".

Applied to the root logger at startup via install_log_filter() so every
logger in the process — Flask, SQLAlchemy, Anthropic SDK, etc. — is covered.

Design notes
------------
- Redaction happens on the *formatted* string so it catches values whether
  they arrive as logger.info("...", arg) positional args or pre-formatted.
- Short values (≤ 8 chars) are left as-is: they are almost certainly
  metadata (mode names, status codes) rather than message content.
- The filter returns True for every record — it never suppresses messages,
  only scrubs them.
"""

import re
import logging

# Matches:  "field": "value"  or  "field":"value"  (JSON double-quoted)
# Also:     field='value'  or  field="value"  (Python repr / keyword args)
# The value capture group is non-greedy so it stops at the first closing quote.
_JSON_RE = re.compile(
    r'(?i)'                                         # case-insensitive field names
    r'("(?:text|content|message|transcript|body|prompt|response)"'     # JSON key (double-quoted)
    r'\s*:\s*")'                                    # colon + opening quote
    r'([^"]{9,})'                                   # value — 9+ chars (skip short metadata)
    r'"',                                           # closing quote
    re.DOTALL,
)

_REPR_RE = re.compile(
    r'(?i)'
    r'\b((?:text|content|message|transcript|body|prompt|response)\s*=\s*[\'"])'   # key='  or  key="
    r'([^\'"]{9,})'                                             # value — 9+ non-quote chars
    r'([\'"])',                                                  # closing quote
)

_REPLACEMENT_JSON = r'\1[REDACTED]"'
_REPLACEMENT_REPR = r'\1[REDACTED]\3'


def _scrub(text: str) -> str:
    text = _JSON_RE.sub(_REPLACEMENT_JSON, text)
    text = _REPR_RE.sub(_REPLACEMENT_REPR, text)
    return text


class SensitiveDataFilter(logging.Filter):
    """Redact PHI-bearing field values from log records in-place."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Collapse msg + args into a single string so the regex has the full
        # formatted message to work with, then clear args to avoid double-formatting.
        if record.args:
            try:
                record.msg = record.msg % record.args
            except Exception:
                # If formatting fails, leave msg as-is; scrub what we have.
                record.msg = str(record.msg)
            record.args = None

        record.msg = _scrub(str(record.msg))
        return True


def install_log_filter() -> None:
    """Attach SensitiveDataFilter to the root logger.

    Called once at app startup.  Because it targets the root logger every
    child logger — including Flask's app.logger, SQLAlchemy's logger, and
    the Anthropic SDK logger — inherits the filter automatically.
    """
    root = logging.getLogger()
    # Avoid adding duplicates if called more than once (e.g. during testing)
    if not any(isinstance(f, SensitiveDataFilter) for f in root.filters):
        root.addFilter(SensitiveDataFilter())
