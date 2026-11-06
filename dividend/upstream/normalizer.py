"""Canonicalize repository URLs to stable host/path identities."""

import re
from urllib.parse import urlsplit


def normalize(url: str | None) -> str | None:
    if type(url) is not str or not url.strip():
        return None
    value = url.strip()
    if value.startswith("git+"):
        value = value[4:]
    scp = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.groups()
    else:
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https", "git", "ssh"} or not parsed.hostname:
                return None
            host, path = parsed.hostname, parsed.path
        except ValueError:
            return None
    host = host.lower().removeprefix("www.")
    path = path.strip("/").lower()
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path or any(part in {".", ".."} for part in path.split("/")):
        return None
    if host in {"github.com", "bitbucket.org"}:
        path = "/".join(path.split("/")[:2])
        if path.count("/") != 1:
            return None
    return f"{host}/{path}"
