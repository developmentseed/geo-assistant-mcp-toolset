"""Statement-shape validation for caller-supplied SQL.

This is defense-in-depth, not the primary control — read
``connection.py``'s module docstring first for the actual security model.
In particular: none of this stops a file-reading table function
(``read_text``, ``read_csv``, ``glob``, ...) called from inside an otherwise
valid ``SELECT``. That class of risk is handled by ``connection.py`` locking
down DuckDB's filesystem access and by the deployment shipping no local
secrets — not by anything in this module.
"""

import re

#: Server-enforced row cap. ``limit`` is clamped into (0, MAX_ROW_LIMIT] before
#: it ever reaches SQL, so a caller cannot bypass it by e.g. omitting a LIMIT
#: clause of their own — see ``connection.wrap_with_limit``.
MAX_ROW_LIMIT = 10_000
DEFAULT_ROW_LIMIT = 1000

#: Wall-clock budget for one query, enforced by a Python-side watchdog that
#: calls ``cursor.interrupt()`` — DuckDB has no native query timeout.
QUERY_TIMEOUT_SECONDS = 30.0

#: Wall-clock budget for the curated geo lookup tools (``geo_tools.py``),
#: which scan the remote Overture places theme bbox-pruned. Measured against
#: the 2026-07-22.0 release: a city-scale bbox name search runs ~160s, a
#: neighborhood-scale category+intersect ~17s (timings in ``connection.py``).
#: Deliberately not applied to caller-supplied SQL, which keeps the 30s
#: budget above — only the code-authored, parameter-bound queries in
#: ``geo_tools`` earn the longer leash.
LOOKUP_TIMEOUT_SECONDS = 240.0

_LEADING_STATEMENT = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

#: Table/scalar functions that are reachable from inside an otherwise valid,
#: single-statement SELECT and disclose runtime configuration or credential
#: state — the "starts with SELECT, one statement" shape check below does not
#: see these, so they need an explicit denylist. Not exhaustive; DuckDB's
#: attack surface here is "whatever a future DuckDB version adds a
#: SELECT-callable introspection function for", so this list is reviewed, not
#: assumed complete.
_DENYLISTED_CALLS = re.compile(
    r"\b(duckdb_secrets|duckdb_settings|pragma_[a-z_]*)\s*\(", re.IGNORECASE
)


def clamp_limit(limit: int) -> int:
    """Clamp a caller-supplied row limit into ``(0, MAX_ROW_LIMIT]``."""
    return max(1, min(limit, MAX_ROW_LIMIT))


def validate_select_only(sql: str) -> str | None:
    """Reject anything that isn't a single ``SELECT``/``WITH`` statement.

    Returns an error detail string if ``sql`` is rejected, or ``None`` if it
    passes this (shallow, syntax-level) check. Rejects:

    - more than one statement (a ``;`` anywhere but a single optional
      trailing one) — defense against ``SELECT 1; ATTACH ...``-style
      smuggling of a second statement;
    - anything not starting with ``SELECT``/``WITH`` — defense against
      ``ATTACH``/``COPY``/``INSTALL``/``PRAGMA``/``CALL``/``SET`` and
      friends;
    - calls to a small denylist of introspection functions (see
      ``_DENYLISTED_CALLS``) that are otherwise perfectly valid inside a
      single ``SELECT``.
    """
    stripped = sql.strip()
    if not stripped:
        return "empty query"

    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        return "only a single statement is allowed (found an embedded ';')"

    if not _LEADING_STATEMENT.match(body):
        return "only SELECT/WITH statements are allowed"

    if match := _DENYLISTED_CALLS.search(body):
        return f"{match.group(1)}() is not allowed"

    return None
