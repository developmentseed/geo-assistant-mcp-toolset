"""Statement-shape validation for caller-supplied SQL.

This is defense-in-depth, not the primary control — read
``connection.py``'s module docstring first for the actual security model.
In particular: none of this stops a file-reading table function
(``read_text``, ``read_csv``, ``glob``, ...) called from inside an otherwise
valid ``SELECT``. That class of risk is handled by ``connection.py`` locking
down DuckDB's filesystem access and by the deployment shipping no local
secrets — not by anything in this module.

The one exception is the ``zarr`` community extension's ``read_zarr*``
functions (see ``connection.py``'s "Zarr" section): that extension does its
own native file I/O and is *not* covered by ``connection.py``'s
``disabled_filesystems`` lockdown, verified empirically. ``_validate_zarr_calls``
below is this toolset's only control against local-path reads through it, so
it is checked here rather than left to layer 2.
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

#: The community ``zarr`` extension's table functions (see ``connection.py``,
#: "Zarr"). Their first argument must be a literal remote URL — local paths
#: reach this extension's own file I/O, which ``disabled_filesystems``
#: cannot block. Checked in two passes below:
#:
#: 1. ``_ZARR_FUNCS`` finds every call to one of these functions and
#:    requires a literal string immediately as its first argument (fails
#:    closed: a non-literal or missing first argument, e.g. a column
#:    reference or ``read_zarr()``, is rejected rather than let through).
#: 2. ``_ZARR_SUFFIXED_LITERAL`` separately catches DuckDB's own replacement
#:    scan: a bare ``FROM '<path>.zarr'`` (no explicit function call at all)
#:    is silently rewritten by the extension into ``read_zarr('<path>.zarr')``
#:    before this module ever sees a function name to match on — see
#:    https://github.com/xqlsystems/duckdb-zarr/blob/main/src/replacement_scan.rs.
#:    So every ``'...zarr'``/``'...zarr/'`` string literal anywhere in the
#:    query is checked for a safe scheme, not just literals that follow one
#:    of the function names above.
#:
#: Same caveat as ``_DENYLISTED_CALLS``: shallow and syntax-level, reviewed
#: not assumed complete. It does not survive string concatenation
#: (``read_zarr('/et' || 'c/passwd')``) or other SQL-level construction of
#: the literal — it stops the straightforward case, not a determined bypass.
_ZARR_FUNCS = re.compile(r"\b(read_zarr(?:_groups|_metadata)?)\s*\(", re.IGNORECASE)
_STRING_LITERAL = re.compile(r"\s*'((?:[^']|'')*)'")
_ZARR_SUFFIXED_LITERAL = re.compile(r"'((?:[^']|'')*?\.zarr/?)'", re.IGNORECASE)
_ALLOWED_ZARR_URL_SCHEMES = ("http://", "https://", "s3://", "gs://", "az://")


def _validate_zarr_calls(body: str) -> str | None:
    """Reject any ``read_zarr*`` call or ``'...zarr'`` literal that isn't a
    literal remote URL. See ``_ZARR_FUNCS`` above for why this exists and
    what it does and doesn't catch.
    """
    for func_match in _ZARR_FUNCS.finditer(body):
        literal = _STRING_LITERAL.match(body, func_match.end())
        if literal is None:
            return (
                f"{func_match.group(1)}() must be called with a literal "
                "http(s):// or s3:// URL as its first argument, not a local path"
            )
        url = literal.group(1).replace("''", "'").lower()
        if not url.startswith(_ALLOWED_ZARR_URL_SCHEMES):
            return (
                f"{func_match.group(1)}() only allows a literal http(s):// "
                "or s3:// URL as its first argument, not a local path"
            )

    for path_match in _ZARR_SUFFIXED_LITERAL.finditer(body):
        path = path_match.group(1).replace("''", "'").lower()
        if not path.startswith(_ALLOWED_ZARR_URL_SCHEMES):
            return (
                "a '...zarr' path is read as a Zarr store by DuckDB and "
                "must be a literal http(s):// or s3:// URL, not a local path"
            )

    return None


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
      single ``SELECT``;
    - a ``read_zarr``/``read_zarr_groups``/``read_zarr_metadata`` call, or a
      bare ``'...zarr'`` literal DuckDB would rewrite into one, whose path
      isn't a literal remote URL (see ``_validate_zarr_calls``).
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

    if detail := _validate_zarr_calls(body):
        return detail

    return None
