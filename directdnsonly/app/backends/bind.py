import os
import subprocess
from loguru import logger
from pathlib import Path
from typing import Dict, List, Optional
from .base import DNSBackend


class BINDBackend(DNSBackend):
    @classmethod
    def get_name(cls) -> str:
        return "bind"

    @classmethod
    def is_available(cls) -> bool:
        try:
            result = subprocess.run(["named", "-v"], capture_output=True, text=True)
            if result.returncode == 0:
                logger.info(f"BIND available: {result.stdout.splitlines()[0]}")
                return True
            return False
        except FileNotFoundError:
            logger.warning("BIND/named not found in PATH")
            return False

    def __init__(self, config: Dict):
        self.zones_dir = Path(config["zones_dir"])
        self.named_conf = Path(config["named_conf"])

        # Safe directory creation handling
        try:
            # Check if it's a symlink first
            if self.zones_dir.is_symlink():
                logger.debug(f"{self.zones_dir} is already a symlink")
            elif not self.zones_dir.exists():
                self.zones_dir.mkdir(parents=True, mode=0o755)
                logger.debug(f"Created zones directory: {self.zones_dir}")
            else:
                logger.debug(f"Directory already exists: {self.zones_dir}")

            # Ensure proper permissions
            os.chmod(self.zones_dir, 0o755)
            logger.debug(f"Using zones directory: {self.zones_dir}")

        except FileExistsError:
            logger.debug(f"Directory already exists (safe to ignore): {self.zones_dir}")
        except Exception as e:
            logger.error(f"Failed to setup zones directory: {e}")
            raise

        # Verify named.conf exists
        if not self.named_conf.exists():
            logger.warning(f"named.conf not found at {self.named_conf}")
            self.named_conf.touch()
            logger.info(f"Created empty named.conf at {self.named_conf}")

        logger.success(f"BIND backend initialized for {self.zones_dir}")

    def write_zone(self, zone_name: str, zone_data: str) -> bool:
        zone_file = self.zones_dir / f"{zone_name}.db"
        try:
            with open(zone_file, "w") as f:
                f.write(zone_data)
            logger.debug(f"Wrote zone file: {zone_file}")
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
                return True
            logger.warning(f"Zone file not found: {zone_file}")
            return False
        except IOError as e:
            logger.error(f"Failed to delete zone file {zone_file}: {e}")
            return False

    def reload_zone(self, zone_name: Optional[str] = None) -> bool:
        try:
            if zone_name:
                cmd = ["rndc", "reload", zone_name]
                logger.debug(f"Reloading single zone: {zone_name}")
            else:
                cmd = ["rndc", "reload"]
                logger.debug("Reloading all zones")

            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            logger.debug(f"BIND reload successful: {result.stdout}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"BIND reload failed: {e.stderr}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during BIND reload: {e}")
            return False

    def zone_exists(self, zone_name: str) -> bool:
        zone_file = self.zones_dir / f"{zone_name}.db"
        exists = zone_file.exists()
        logger.debug(f"Zone existence check for {zone_name}: {exists}")
        return exists

    def update_named_conf(self, zones: List[str]) -> bool:
        try:
            with open(self.named_conf, "w") as f:
                for zone in zones:
                    zone_file = self.zones_dir / f"{zone}.db"
                    f.write(f'zone "{zone}" {{ type master; file "{zone_file}"; }};\n')
            logger.debug(f"Updated named.conf: {self.named_conf}")
            return True
        except IOError as e:
            logger.error(f"Failed to update named.conf: {e}")
            return False
