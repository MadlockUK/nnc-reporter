"""Guards for addresses the server is asked to fetch.

In the hosted app the server fetches things on people's behalf - a Google My
Map, the photos its pins link to. A link inside an imported file must never be
able to point the server at itself or at the private network it sits on, so
anything fetched from a user-supplied address goes through `check_url` first.
"""
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURL(ValueError):
    pass


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def check_url(url: str, hosts: tuple = ()) -> str:
    """Return the URL if it is https, on an allowed host (when `hosts` is given,
    matching the host or any subdomain of it) and resolves only to public
    addresses. Raises UnsafeURL otherwise."""
    parts = urlparse((url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeURL("only https:// addresses can be fetched")
    host = parts.hostname.lower().rstrip(".")
    if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
        raise UnsafeURL(f"{host} is not a host this tool fetches from")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve {host}: {e}") from e
    if not infos or not all(_public(i[4][0]) for i in infos):
        raise UnsafeURL(f"{host} does not resolve to a public address")
    return url


GOOGLE_HOSTS = ("google.com", "googleusercontent.com", "ggpht.com", "gstatic.com")


def get(url: str, hosts: tuple = (), max_hops: int = 4, **kwargs):
    """requests.get that checks the address - and every redirect it is sent on -
    with check_url before going there."""
    import requests
    from urllib.parse import urljoin
    kwargs["allow_redirects"] = False
    for _ in range(max_hops + 1):
        check_url(url, hosts)
        resp = requests.get(url, **kwargs)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            nxt = resp.headers.get("Location")
            if not nxt:
                return resp
            url = urljoin(url, nxt)
            continue
        return resp
    raise UnsafeURL("too many redirects")
