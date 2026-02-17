from typing import Optional, Dict, Set, Tuple, List

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    Text,
    Boolean,
    DateTime,
    func,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from loguru import logger
from .base import DNSBackend
from config import config
import time

Base = declarative_base()


class Domain(Base):
    __tablename__ = "domains"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, index=True, unique=True)
    master = Column(String(128), nullable=True)
    last_check = Column(Integer, nullable=True)
    type = Column(String(6), nullable=False, default="NATIVE")
    notified_serial = Column(Integer, nullable=True)
    account = Column(String(40), nullable=True)


class Record(Base):
    __tablename__ = "records"
    id = Column(Integer, primary_key=True)
    domain_id = Column(Integer, nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    type = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    ttl = Column(Integer, nullable=True)
    prio = Column(Integer, nullable=True)
    change_date = Column(Integer, nullable=True)
    disabled = Column(Boolean, nullable=False, default=False)
    ordername = Column(String(255), nullable=True)
    auth = Column(Boolean, nullable=False, default=True)


class PowerDNSMySQLBackend(DNSBackend):
    @classmethod
    def get_name(cls) -> str:
        return "powerdns_mysql"

    @classmethod
    def is_available(cls) -> bool:
        try:
            import pymysql

            return True
        except ImportError:
            logger.warning("PyMySQL not available - PowerDNS MySQL backend disabled")
            return False

    @staticmethod
    def ensure_fqdn(name: str, zone_name: str) -> str:
        """Ensure name is fully qualified for PowerDNS"""
        if name == "@" or name == "":
            return zone_name
        elif name.endswith("."):
            return name.rstrip(".")
        elif name == zone_name:
            return name
        else:
            return f"{name}.{zone_name}"

    def __init__(self, config: dict = None):
        c = config or config.get("dns.backends.powerdns_mysql")
        self.engine = create_engine(
            f"mysql+pymysql://{c['username']}:{c['password']}@"
            f"{c['host']}:{c['port']}/{c['database']}",
            pool_pre_ping=True,
        )
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        Base.metadata.create_all(self.engine)
        logger.info(f"Initialized PowerDNS MySQL backend for {c['database']}")

    def _ensure_domain_exists(self, session, zone_name: str) -> Domain:
        """Ensure domain exists and return domain object"""
        domain = session.query(Domain).filter_by(name=zone_name).first()
        if not domain:
            domain = Domain(name=zone_name, type="NATIVE")
            session.add(domain)
            session.flush()  # Flush to get the domain ID
            logger.info(f"Created new domain: {zone_name}")
        return domain

    def _parse_soa_content(self, soa_content: str) -> Dict[str, str]:
        """Parse SOA record content into components"""
        parts = soa_content.split()
        if len(parts) >= 7:
            return {
                "primary_ns": parts[0],
                "hostmaster": parts[1],
                "serial": parts[2],
                "refresh": parts[3],
                "retry": parts[4],
                "expire": parts[5],
                "minimum": parts[6],
            }
        return {}

    def write_zone(self, zone_name: str, zone_data: str) -> bool:
        from dns import zone as dns_zone_module
        from dns.rdataclass import IN

        session = self.Session()
        try:
            # Ensure domain exists
            domain = self._ensure_domain_exists(session, zone_name)

            # Get existing records for this domain
            existing_records = {
                (r.name, r.type): r
                for r in session.query(Record).filter_by(domain_id=domain.id).all()
            }

            # Parse the zone data
            dns_zone = dns_zone_module.from_text(zone_data, check_origin=False)

            # Track records we process
            current_records: Set[Tuple[str, str]] = set()
            changes = {"added": 0, "updated": 0, "removed": 0}
            current_time = int(time.time())

            # Process all records
            for name, ttl, rdata in dns_zone.iterate_rdatas():
                if rdata.rdclass != IN:
                    continue

                record_name = self.ensure_fqdn(str(name), zone_name)
                record_type = rdata.rdtype.name
                record_content = rdata.to_text()
                record_ttl = ttl
                record_prio = None

                # Handle MX records priority
                if record_type == "MX":
                    parts = record_content.split(" ", 1)
                    if len(parts) == 2:
                        record_prio = int(parts[0])
                        record_content = parts[1]

                # Handle SRV records priority and other fields
                elif record_type == "SRV":
                    parts = record_content.split(" ", 3)
                    if len(parts) == 4:
                        record_prio = int(parts[0])
                        record_content = f"{parts[1]} {parts[2]} {parts[3]}"

                # Ensure CNAME and other records have proper FQDN format
                if record_type in ["CNAME", "MX", "NS"]:
                    if not record_content.endswith(".") and record_content != "@":
                        if record_content == "@":
                            record_content = zone_name
                        elif "." not in record_content:
                            record_content = f"{record_content}.{zone_name}"

                key = (record_name, record_type)
                current_records.add(key)

                if key in existing_records:
                    # Update existing record if needed
                    record = existing_records[key]
                    if (
                        record.content != record_content
                        or record.ttl != record_ttl
                        or record.prio != record_prio
                    ):
                        record.content = record_content
                        record.ttl = record_ttl
                        record.prio = record_prio
                        record.change_date = current_time
                        record.disabled = False
                        changes["updated"] += 1
                else:
                    # Add new record
                    new_record = Record(
                        domain_id=domain.id,
                        name=record_name,
                        type=record_type,
                        content=record_content,
                        ttl=record_ttl,
                        prio=record_prio,
                        change_date=current_time,
                        disabled=False,
                        auth=True,
                    )
                    session.add(new_record)
                    changes["added"] += 1

            # Remove deleted records
            for key in set(existing_records.keys()) - current_records:
                session.delete(existing_records[key])
                changes["removed"] += 1

            session.commit()
            logger.success(
                f"Zone {zone_name} updated: "
                f"+{changes['added']} ~{changes['updated']} -{changes['removed']}"
            )
            return True

        except Exception as e:
            session.rollback()
            logger.error(f"Zone update failed for {zone_name}: {e}")
            return False
        finally:
            session.close()

    def delete_zone(self, zone_name: str) -> bool:
        session = self.Session()
        try:
            # First find the domain
            domain = session.query(Domain).filter_by(name=zone_name).first()
            if not domain:
                logger.warning(f"Domain {zone_name} not found for deletion")
                return False

            # Delete all records associated with the domain
            count = session.query(Record).filter_by(domain_id=domain.id).delete()

            # Delete the domain itself
            session.delete(domain)
            session.commit()

            logger.info(f"Deleted domain {zone_name} with {count} records")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Domain deletion failed for {zone_name}: {e}")
            return False
        finally:
            session.close()

    def reload_zone(self, zone_name: Optional[str] = None) -> bool:
        """PowerDNS reload - could trigger pdns_control reload if needed"""
        if zone_name:
            logger.debug(f"PowerDNS reload triggered for zone {zone_name}")
            # Optional: Call pdns_control reload-zones here if needed
            # subprocess.run(['pdns_control', 'reload-zones'], check=True)
        else:
            logger.debug("PowerDNS reload triggered for all zones")
            # Optional: Call pdns_control reload here if needed
            # subprocess.run(['pdns_control', 'reload'], check=True)
        return True

    def zone_exists(self, zone_name: str) -> bool:
        session = self.Session()
        try:
            exists = session.query(Domain).filter_by(name=zone_name).first() is not None
            logger.debug(f"Zone existence check for {zone_name}: {exists}")
            return exists
        except Exception as e:
            logger.error(f"Zone existence check failed for {zone_name}: {e}")
            return False
        finally:
            session.close()

    def get_zone_records(self, zone_name: str) -> List[Dict]:
        """Get all records for a zone - useful for debugging/inspection"""
        session = self.Session()
        try:
            domain = session.query(Domain).filter_by(name=zone_name).first()
            if not domain:
                return []

            records = session.query(Record).filter_by(domain_id=domain.id).all()
            return [
                {
                    "name": r.name,
                    "type": r.type,
                    "content": r.content,
                    "ttl": r.ttl,
                    "prio": r.prio,
                    "disabled": r.disabled,
                }
                for r in records
            ]
        except Exception as e:
            logger.error(f"Failed to get records for {zone_name}: {e}")
            return []
        finally:
            session.close()

    def set_record_status(
        self, zone_name: str, record_name: str, record_type: str, disabled: bool
    ) -> bool:
        """Enable/disable specific records"""
        session = self.Session()
        try:
            domain = session.query(Domain).filter_by(name=zone_name).first()
            if not domain:
                logger.warning(f"Domain {zone_name} not found")
                return False

            full_name = self.ensure_fqdn(record_name, zone_name)
            record = (
                session.query(Record)
                .filter_by(domain_id=domain.id, name=full_name, type=record_type)
                .first()
            )

            if not record:
                logger.warning(
                    f"Record {full_name} {record_type} not found in {zone_name}"
                )
                return False

            record.disabled = disabled
            record.change_date = int(time.time())
            session.commit()

            status = "disabled" if disabled else "enabled"
            logger.info(f"Record {full_name} {record_type} {status} in {zone_name}")
            return True

        except Exception as e:
            session.rollback()
            logger.error(f"Failed to set record status: {e}")
            return False
        finally:
            session.close()
