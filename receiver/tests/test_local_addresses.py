"""Tests for detecting this host's own addresses.

Used to narrow the syslog exclusion to traffic aimed at this collector, rather
than to every packet on port 514.
"""

import socket
from unittest.mock import patch

from net_identity import local_addresses


class TestLocalAddresses:
    def test_returns_strings(self):
        for address in local_addresses():
            assert isinstance(address, str)

    def test_excludes_loopback(self):
        """The gateway cannot send to our loopback, so it is never the target."""
        assert '127.0.0.1' not in local_addresses()
        assert '::1' not in local_addresses()

    def test_is_deduplicated(self):
        addresses = local_addresses()
        assert len(addresses) == len(set(addresses))

    def test_survives_a_broken_hostname(self):
        """An unresolvable hostname is common in a container."""
        with patch('socket.getaddrinfo', side_effect=socket.gaierror):
            assert isinstance(local_addresses(), list)

    def test_survives_no_network(self):
        with patch('socket.socket', side_effect=OSError):
            assert isinstance(local_addresses(), list)

    def test_is_cached(self):
        """Called on every settings read; the answer changes only on a restart."""
        first = local_addresses()
        with patch('socket.getaddrinfo', side_effect=AssertionError('should not re-resolve')):
            assert local_addresses() == first
