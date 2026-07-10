"""Shared Git credential injection helpers."""

from __future__ import annotations

import base64
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit

_GITHUB_EXTRAHEADER_KEY = "http.https://github.com/.extraheader"
_MIN_GIT_CONFIG_ENV = (2, 31, 0)
_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
_GIT_ENV_SUPPORTED: bool | None = None


def _auth_b64(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _extra_header(token: str) -> str:
    return f"Authorization: Basic {_auth_b64(token)}"


def _parse_git_version(output: str) -> tuple[int, int, int] | None:
    match = _GIT_VERSION_RE.search(output)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _git_env_supported() -> bool:
    global _GIT_ENV_SUPPORTED
    if _GIT_ENV_SUPPORTED is not None:
        return _GIT_ENV_SUPPORTED
    try:
        out = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        _GIT_ENV_SUPPORTED = False
        return _GIT_ENV_SUPPORTED
    version = _parse_git_version(out.stdout or out.stderr or "")
    _GIT_ENV_SUPPORTED = bool(version and version >= _MIN_GIT_CONFIG_ENV)
    return _GIT_ENV_SUPPORTED


def clean_url(url: str) -> str:
    """Return url with userinfo removed, preserving host, port, path, query and fragment."""
    parts = urlsplit(url)
    if not parts.netloc or "@" not in parts.netloc:
        return url
    netloc = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _github_url(url: str | None) -> bool:
    if url is None:
        return True
    parts = urlsplit(clean_url(url))
    return parts.scheme == "https" and (parts.hostname or "").lower() == "github.com"


def make_env(token: str | None, url: str | None = None) -> dict[str, str]:
    """Build GIT_CONFIG_* env for GitHub auth without putting the token in argv.

    The generated config starts at index 0 and intentionally overwrites any parent
    GIT_CONFIG_* values when callers merge via ``{**os.environ, **make_env(...)}``.
    """
    if not token or not _github_url(url) or not _git_env_supported():
        return {}
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": _GITHUB_EXTRAHEADER_KEY,
        "GIT_CONFIG_VALUE_1": _extra_header(token),
    }


def git_cred_argv(token: str | None, url: str | None = None) -> list[str]:
    """Fallback argv config for Git versions without GIT_CONFIG_* env support."""
    if not token or not _github_url(url) or _git_env_supported():
        return []
    return [
        "-c",
        "credential.helper=",
        "-c",
        f"{_GITHUB_EXTRAHEADER_KEY}={_extra_header(token)}",
    ]


__all__ = ["clean_url", "git_cred_argv", "make_env"]
