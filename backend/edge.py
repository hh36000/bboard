"""Decide which proxy-supplied request metadata the backend may trust."""
from functools import lru_cache
import hmac
import ipaddress
import os


def trusted_edge(headers):
    """The method-preserving proxy proves itself with BBOARD_EDGE_TOKEN."""
    expected = os.environ.get('BBOARD_EDGE_TOKEN', '')
    supplied = headers.get('x-bboard-edge', '')
    return bool(expected) and hmac.compare_digest(expected.encode(), supplied.encode())


@lru_cache(maxsize=8)
def _proxy_networks(value):
    if value.strip() == '*':
        return '*'
    return tuple(ipaddress.ip_network(item.strip(), strict=False) for item in value.split(',') if item.strip())


def trusted_forwarder(peer):
    """Only peers listed in BBOARD_TRUSTED_PROXIES may supply X-Forwarded-For.

    '*' trusts every peer. Use it only when the platform itself appends the
    caller's address and the backend cannot be reached any other way.
    """
    networks = _proxy_networks(os.environ.get('BBOARD_TRUSTED_PROXIES', ''))
    if networks == '*':
        return True
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in networks)
