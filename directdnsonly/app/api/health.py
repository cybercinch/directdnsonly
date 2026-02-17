import cherrypy
from loguru import logger


class HealthAPI:
    def __init__(self, backend_registry):
        self.registry = backend_registry

    @cherrypy.expose
    def health(self):
        status = {"status": "OK", "backends": []}

        for name, backend in self.registry.get_available_backends().items():
            status["backends"].append(
                {
                    "name": name,
                    "status": (
                        "active" if backend().zone_exists("test") else "unavailable"
                    ),
                }
            )

        logger.debug("Health check performed")
        return status
