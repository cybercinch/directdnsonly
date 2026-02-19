import os
import re
import subprocess
from loguru import logger
from pathlib import Path
from typing import Dict, List, Optional
from .base import DNSBackend


class NSDBackend(DNSBackend):
    """DNS backend for NSD (Name Server Daemon) by NLnet Labs.

    Zone files use the same RFC 1035 format as BIND. NSD is reloaded via
    ``nsd-control reload`` after each write. Zone registration is managed in a
    dedicated include file so the main ``nsd.conf`` is never modified by the
    application.
    """

    @classmethod
    def get_name(cls) -> str:
        return "nsd"

    @classmethod
    def is_available(cls) -> bool:
        try:
            result = subprocess.run(
                ["nsd-control", "status"],
                capture_output=True,
                text=True,
            )
            # nsd-control exits 0 when NSD is running, non-zero otherwise.
            # Either way, a non-FileNotFoundError means the binary is present.
            logger.info("NSD available (nsd-control found)")
            return True
        except FileNotFoundError:
            logger.warning("NSD not found in PATH — nsd-control missing")
            return False

    def __init__(self, config: Dict):
        super().__init__(config)
        self.zones_dir = Path(config.get("zones_dir", "/etc/nsd/zones"))
        self.nsd_conf = Path(
            config.get("nsd_conf", "/etc/nsd/nsd.conf.d/zones.conf")
        )

        # Ensure zones directory exists
        try:
            if self.zones_dir.is_symlink():
                logger.debug(f"{self.zones_dir} is already a symlink")
            elif not self.zones_dir.exists():
                self.zones_dir.mkdir(parents=True, mode=0o755)
                logger.debug(f"Created zones directory: {self.zones_dir}")
            os.chmod(self.zones_dir, 0o755)
        except FileExistsError:
            pass
        except Exception as e:
            logger.error(f"Failed to setup zones directory: {e}")
            raise

        # Ensure the conf include directory and file exist
        self.nsd_conf.parent.mkdir(parents=True, exist_ok=True)
        if not self.nsd_conf.exists():
            self.nsd_conf.touch()
            logger.info(f"Created empty NSD zone conf: {self.nsd_conf}")

        logger.success(
            f"NSD backend initialized — zones: {self.zones_dir}, "
            f"conf: {self.nsd_conf}"
        )

    # ------------------------------------------------------------------
    # Core backend interface
    # ------------------------------------------------------------------

    def write_zone(self, zone_name: str, zone_data: str) -> bool:
        zone_file = self.zones_dir / f"{zone_name}.db"
        try:
            zone_file.write_text(zone_data)
            logger.debug(f"Wrote zone file: {zone_file}")
            self._ensure_zone_in_conf(zone_name)
            return True
        except IOError as e:
            logger.error(f"Failed to write zone file {zone_file}: {e}")
            return False

    def delete_zone(self, zone_name: str) -> bool:
        zone_file = self.zones_dir / f"{zone_name}.db"
        try:
            if zone_file.exists():
                zone_file.unlink()
                logger.debug(f"Deleted zone file: {zone_file}")
            else:
                logger.warning(f"Zone file not found: {zone_file}")
                return False
            self._remove_zone_from_conf(zone_name)
            return True
        except IOError as e:
            logger.error(f"Failed to delete zone {zone_name}: {e}")
            return False

    def reload_zone(self, zone_name: Optional[str] = None) -> bool:
        try:
            if zone_name:
                cmd = ["nsd-control", "reload", zone_name]
                logger.debug(f"Reloading single zone: {zone_name}")
            else:
                cmd = ["nsd-control", "reload"]
                logger.debug("Reloading all zones")

            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            logger.debug(f"NSD reload successful: {result.stdout.strip()}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"NSD reload failed: {e.stderr.strip()}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during NSD reload: {e}")
            return False

    def zone_exists(self, zone_name: str) -> bool:
        exists = (self.zones_dir / f"{zone_name}.db").exists()
        logger.debug(f"Zone existence check for {zone_name}: {exists}")
        return exists

    # ------------------------------------------------------------------
    # NSD conf file management
    # ------------------------------------------------------------------

    def update_nsd_conf(self, zones: List[str]) -> bool:
        """Rewrite the NSD zones include file with exactly the given zone list.

        Equivalent to BINDBackend.update_named_conf — full replacement from a
        known-good source list.
        """
        try:
            lines = []
            for zone in zones:
                zone_file = self.zones_dir / f"{zone}.db"
                lines.append(
                    f'\nzone:\n    name: "{zone}"\n    zonefile: "{zone_file}"\n'
                )
            self.nsd_conf.write_text("".join(lines))
            logger.debug(f"Rewrote NSD zone conf: {self.nsd_conf}")
            return True
        except IOError as e:
            logger.error(f"Failed to update NSD zone conf: {e}")
            return False

    def _ensure_zone_in_conf(self, zone_name: str) -> None:
        """Append a zone stanza to the NSD conf file if it is not already present."""
        zone_file = self.zones_dir / f"{zone_name}.db"
        stanza = f'\nzone:\n    name: "{zone_name}"\n    zonefile: "{zone_file}"\n'

        content = self.nsd_conf.read_text() if self.nsd_conf.exists() else ""
        if f'name: "{zone_name}"' not in content:
            with open(self.nsd_conf, "a") as f:
                f.write(stanza)
            logger.debug(f"Added zone {zone_name} to NSD conf")

    def _remove_zone_from_conf(self, zone_name: str) -> None:
        """Remove a zone stanza from the NSD conf file."""
        if not self.nsd_conf.exists():
            return
        content = self.nsd_conf.read_text()
        pattern = (
            r'\nzone:\n    name: "'
            + re.escape(zone_name)
            + r'"\n    zonefile: "[^"]+"\n'
        )
        new_content = re.sub(pattern, "", content)
        if new_content != content:
            self.nsd_conf.write_text(new_content)
            logger.debug(f"Removed zone {zone_name} from NSD conf")
