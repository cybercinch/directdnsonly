from dns import zone, name
from dns.rdataclass import IN
from dns.exception import DNSException
from loguru import logger


def validate_and_normalize_zone(zone_data: str, domain_name: str) -> str:
    """
    Normalize zone file content and ensure proper origin handling
    Returns normalized zone data
    Raises DNSException on validation failure
    """
    # Ensure domain ends with dot
    if not domain_name.endswith("."):
        domain_name = f"{domain_name}."

    # Add $ORIGIN if missing
    if "$ORIGIN" not in zone_data:
        zone_data = f"$ORIGIN {domain_name}\n{zone_data}"

    # Add $TTL if missing
    if "$TTL" not in zone_data:
        zone_data = f"$TTL 300\n{zone_data}"

    # Validate the zone
    try:
        zone.from_text(
            zone_data, origin=name.from_text(domain_name), check_origin=False
        )
        return zone_data
    except DNSException as e:
        logger.error(f"Zone validation failed: {e}")
        raise ValueError(f"Invalid zone data: {str(e)}")


def count_zone_records(zone_data: str, domain_name: str) -> int:
    """Count the number of individual DNS records in a parsed BIND zone file.

    This counts every individual resource record (each A, AAAA, MX, TXT, etc.)
    the same way the CoreDNS MySQL backend stores them — one row per record.

    Args:
        zone_data: The raw or normalized BIND zone file content
        domain_name: The domain name for the zone

    Returns:
        The total number of individual records in the zone
    """
    if not domain_name.endswith("."):
        domain_name = f"{domain_name}."

    try:
        dns_zone = zone.from_text(
            zone_data, origin=name.from_text(domain_name), check_origin=False
        )

        count = 0
        for _, _, rdata in dns_zone.iterate_rdatas():
            if rdata.rdclass == IN:
                count += 1

        logger.debug(f"Source zone {domain_name} contains {count} records")
        return count

    except DNSException as e:
        logger.error(f"Failed to count records for {domain_name}: {e}")
        return -1
