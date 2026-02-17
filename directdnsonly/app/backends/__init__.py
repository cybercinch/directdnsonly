from typing import Dict, Type, Optional
from .base import DNSBackend
from .bind import BINDBackend
from .coredns_mysql import CoreDNSMySQLBackend
from directdnsonly.config import config
from loguru import logger


class BackendRegistry:
    def __init__(self):
        self._backend_types = {
            "bind": BINDBackend,
            "coredns_mysql": CoreDNSMySQLBackend,
        }
        self._backend_instances: Dict[str, DNSBackend] = {}
        self._initialized = False

    def _initialize_backends(self):
        """Initialize and cache all enabled backend instances"""
        if self._initialized:
            return

        try:
            logger.debug("Attempting to load backend configurations")
            backend_configs = config.get("dns")
            if not backend_configs:
                logger.warning("No 'dns' configuration found")
                self._initialized = True
                return

            backend_configs = backend_configs.get("backends")
            if not backend_configs:
                logger.warning("No 'dns.backends' configuration found")
                self._initialized = True
                return

            logger.debug(f"Found backend configs: {backend_configs}")

            for instance_name, instance_config in backend_configs.items():
                logger.debug(f"Processing backend instance: {instance_name}")
                backend_type = instance_config.get("type")

                if not backend_type:
                    logger.warning(
                        f"No type specified for backend instance: {instance_name}"
                    )
                    continue

                if backend_type not in self._backend_types:
                    logger.warning(
                        f"Unknown backend type '{backend_type}' for instance: {instance_name}"
                    )
                    continue

                backend_class = self._backend_types[backend_type]
                if not backend_class.is_available():
                    logger.warning(
                        f"Backend {backend_type} is not available for instance: {instance_name}"
                    )
                    continue

                enabled = instance_config.get("enabled", False)
                if not enabled:
                    logger.debug(f"Backend instance {instance_name} is disabled")
                    continue

                logger.debug(
                    f"Initializing backend instance {instance_name} of type {backend_type}"
                )
                try:
                    backend = backend_class(instance_config)
                    self._backend_instances[instance_name] = backend
                    logger.info(
                        f"Successfully initialized backend instance: {instance_name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to initialize backend instance {instance_name}: {e}"
                    )

        except Exception as e:
            logger.error(f"Error loading backend configurations: {e}")

        self._initialized = True

    def get_available_backends(self) -> Dict[str, DNSBackend]:
        """Return cached backend instances, initializing on first call"""
        self._initialize_backends()
        return self._backend_instances
