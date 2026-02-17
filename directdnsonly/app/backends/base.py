from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Tuple


class DNSBackend(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.instance_name = config.get("instance_name", self.get_name())

    @classmethod
    @abstractmethod
    def get_name(cls) -> str:
        """Return the backend type name"""
        pass

    @property
    def instance_id(self) -> str:
        """Return the unique instance identifier"""
        return self.instance_name

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        pass

    @abstractmethod
    def write_zone(self, zone_name: str, zone_data: str) -> bool:
        pass

    @abstractmethod
    def delete_zone(self, zone_name: str) -> bool:
        pass

    @abstractmethod
    def reload_zone(self, zone_name: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    def zone_exists(self, zone_name: str) -> bool:
        pass

    def verify_zone_record_count(
        self, zone_name: str, expected_count: int
    ) -> Tuple[bool, int]:
        """Verify the record count in this backend matches the expected count
        from the source zone file.

        Args:
            zone_name: The zone to verify
            expected_count: The number of records parsed from the source zone

        Returns:
            Tuple of (matches: bool, actual_count: int)
        """
        raise NotImplementedError(
            f"Backend {self.get_name()} does not implement record count verification"
        )

    def reconcile_zone_records(
        self, zone_name: str, zone_data: str
    ) -> Tuple[bool, int]:
        """Reconcile backend records against the authoritative BIND zone from
        DirectAdmin. Any records in the backend that are not present in the
        source zone will be removed.

        Args:
            zone_name: The zone to reconcile
            zone_data: The raw BIND zone file content (authoritative source)

        Returns:
            Tuple of (success: bool, records_removed: int)
        """
        raise NotImplementedError(
            f"Backend {self.get_name()} does not implement zone reconciliation"
        )
