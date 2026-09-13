"""
IANA Service Name Lookup

Maps port numbers and transport protocols to IANA-registered service names.
CSV source: https://www.iana.org/assignments/service-names-port-numbers/

The CSV is bundled at build time in receiver/data/ and copied to /app/data/ by Docker.
"""
import csv
import logging
from pathlib import Path
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# Global service mappings: (port, protocol) -> name / description
_SERVICE_MAP: Dict[Tuple[int, str], str] = {}
_SERVICE_DESC_MAP: Dict[Tuple[int, str], str] = {}

# Display-friendly overrides for IANA service names
_DISPLAY_OVERRIDES = {
    'domain': 'DNS',
}

def _load_service_maps():
    """
    Load IANA service names and descriptions from CSV at module initialization.

    Returns (name_map, desc_map) dicts keyed by (port, protocol).
    name_map values are short service names (e.g. "http", "ssh").
    desc_map values are longer descriptions (e.g. "World Wide Web HTTP").
    Gracefully degrades to empty dicts if CSV is missing or malformed.
    """
    name_map = {}
    desc_map = {}
    csv_path = Path(__file__).parent / 'data' / 'service-names-port-numbers.csv'

    if not csv_path.exists():
        logger.warning(f"IANA service CSV not found at {csv_path} — service name lookups will return None")
        return name_map, desc_map

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Extract fields
                service_name = row.get('Service Name', '').strip()
                description = row.get('Description', '').strip()
                port_str = row.get('Port Number', '').strip()
                protocol = row.get('Transport Protocol', '').strip().lower()
                reference = row.get('Reference', '').strip()

                # Skip entries without a usable name or port
                if not (service_name or description) or not port_str:
                    continue

                # Parse port number (can be a range like "80-90", we take first)
                try:
                    if '-' in port_str:
                        port = int(port_str.split('-')[0])
                    else:
                        port = int(port_str)
                except ValueError:
                    continue

                # Skip invalid protocols
                if protocol not in ('tcp', 'udp', 'sctp', 'dccp'):
                    continue

                key = (port, protocol)
                short = service_name or description
                long = description or service_name

                # Prefer RFC-standardized entries over non-standard ones
                if key not in name_map or (reference and 'RFC' in reference.upper()):
                    name_map[key] = short
                    if long != short:
                        desc_map[key] = long
                    elif key in desc_map and name_map[key] != short:
                        # New RFC entry replaced name; clear stale description if same
                        pass

        logger.info(f"Loaded {len(name_map)} IANA service name mappings from {csv_path}")

    except Exception as e:
        logger.error(
            f"Failed to parse IANA service CSV at {csv_path}: {e} — "
            f"returning {len(name_map)} entries parsed before error"
        )

    return name_map, desc_map

# Initialize at module load
_SERVICE_MAP, _SERVICE_DESC_MAP = _load_service_maps()

def get_service_mappings() -> Dict[Tuple[int, str], str]:
    """Return the full service name mapping dictionary.

    Returns:
        Dict keyed by (port, protocol) -> short service name.
    """
    return _SERVICE_MAP


def get_service_description(port: Optional[int], protocol: Optional[str] = 'tcp') -> Optional[str]:
    """Return IANA service description (longer form) for the given port and protocol.

    Returns the description only when it differs from the short name.
    Returns None if no description exists or port is None.
    """
    if port is None:
        return None
    normalized_protocol = (protocol or 'tcp').lower()
    return _SERVICE_DESC_MAP.get((port, normalized_protocol))

def ports_for_service(name: str) -> list[int]:
    """Destination ports that resolve to this service name.

    The reverse of get_service_name, used by the `service` filter now that
    service_name is no longer a stored column: a filter for "https" becomes
    `dst_port IN (443, ...)`, which hits the port index instead of scanning a
    text column.

    Matching is case-insensitive and covers the display overrides, so the name
    the UI shows is the name that filters.
    """
    if not name:
        return []
    wanted = name.strip().lower()
    ports = {
        port for (port, _proto), service in _SERVICE_MAP.items()
        if service and service.lower() == wanted
    }
    ports.update(
        port for (port, _proto), service in _SERVICE_MAP.items()
        if _DISPLAY_OVERRIDES.get(service, service).lower() == wanted
    )
    return sorted(ports)


def get_service_name(port: Optional[int], protocol: Optional[str] = 'tcp') -> Optional[str]:
    """
    Return IANA service name for the given port and protocol.

    Args:
        port: Port number (e.g., 80, 443). Can be None for non-port protocols like ICMP.
        protocol: Transport protocol ('TCP', 'UDP', etc.). Case-insensitive. Defaults to 'tcp'.

    Returns:
        Service name string if found, otherwise None.

    Examples:
        >>> get_service_name(80, 'TCP')
        'http'
        >>> get_service_name(443, 'tcp')
        'https'
        >>> get_service_name(53, 'udp')
        'domain'
        >>> get_service_name(None, 'icmp')
        None
    """
    if port is None:
        return None

    # Normalize protocol to lowercase (parsers.py extracts as uppercase from iptables)
    normalized_protocol = (protocol or 'tcp').lower()

    name = _SERVICE_MAP.get((port, normalized_protocol))
    return _DISPLAY_OVERRIDES.get(name, name) if name else None
