"""This host's own network addresses.

Used to narrow the syslog exclusion: the gateway sends its logs to one of these,
so hiding "traffic to port 514 at these addresses" removes the collector's own
noise without hiding syslog running between two other hosts.
"""

import functools
import logging
import socket

logger = logging.getLogger('net_identity')

_LOOPBACK = {'127.0.0.1', '::1'}


@functools.lru_cache(maxsize=1)
def local_addresses() -> list[str]:
    """Every non-loopback address this host answers on.

    Cached for the life of the process: a container's addresses change with a
    restart, and this is consulted on every settings read.

    Both probes are best-effort. In a container the hostname often does not
    resolve, and the UDP probe needs no reachable network — connect() on a
    datagram socket only selects a route — but either can still fail on an
    unusual network setup, and neither failing is worth an error.
    """
    found = set()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except (socket.gaierror, OSError, UnicodeError):
        logger.debug("Could not resolve own hostname", exc_info=True)

    # Ask the routing table which source address a packet would leave from.
    for family, probe in ((socket.AF_INET, ('192.0.2.1', 9)),
                          (socket.AF_INET6, ('2001:db8::1', 9))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.1)
                sock.connect(probe)
                found.add(sock.getsockname()[0])
        except (OSError, AttributeError):
            logger.debug("Route probe failed for %s", family, exc_info=True)

    # Strip the zone id an IPv6 link-local address carries (fe80::1%eth0).
    cleaned = {a.split('%')[0] for a in found}
    return sorted(cleaned - _LOOPBACK)
