from loguru import logger
import cherrypy
from app.backends import BackendRegistry
from app.api.admin import DNSAdminAPI
from app.api.health import HealthAPI
from app import configure_logging
from worker import WorkerManager
from directdnsonly.config import config
from directdnsonly.app.db import connect
import importlib.metadata

app_version = importlib.metadata.version("directdnsonly")


class Root:
    pass


def main():
    try:
        # Initialize logging
        configure_logging()
        logger.info("Starting DaDNS server initialization")

        # Initialize backend registry
        registry = BackendRegistry()
        available_backends = registry.get_available_backends()
        logger.info(f"Available backend instances: {list(available_backends.keys())}")

        global session
        try:
            session = connect(config.get("datastore.type"))
        except Exception as e:
            logger.error(str(e))
            print("ERROR: " + str(e))
            exit(1)
        logger.info("Database Connected!")

        # Setup worker manager
        worker_manager = WorkerManager(
            queue_path=config.get("queue_location"), backend_registry=registry
        )
        worker_manager.start()
        logger.info(
            f"Worker manager started with queue path: {config.get('queue_location')}"
        )

        # Configure CherryPy
        user_password_dict = {
            config.get_string("app.auth_username"): config.get_string("app.auth_password")
        }
        check_password = cherrypy.lib.auth_basic.checkpassword_dict(user_password_dict)

        cherrypy.config.update(
            {
                "server.socket_host": "0.0.0.0",
                "server.socket_port": config.get_int("app.listen_port"),
                "tools.proxy.on": config.get_bool("app.proxy_support"),
                "tools.proxy.base": config.get_string("app.proxy_support_base"),
                "tools.auth_basic.on": True,
                "tools.auth_basic.realm": "dadns",
                "tools.auth_basic.checkpassword": check_password,
                "tools.response_headers.on": True,
                "tools.response_headers.headers": [
                    ("Server", "DirectDNS v" + app_version)
                ],
                "environment": config.get("environment"),
            }
        )

        if config.get_bool("app.ssl_enable"):
            cherrypy.config.update(
                {
                    "server.ssl_module": "builtin",
                    "server.ssl_certificate": config.get("app.ssl_cert"),
                    "server.ssl_private_key": config.get("app.ssl_key"),
                    "server.ssl_certificate_chain": config.get("ssl_bundle"),
                }
            )

        # cherrypy.log.error_log.propagate = False
        if config.get_string("app.log_level").upper() != "DEBUG":
            cherrypy.log.access_log.propagate = False

        # Mount applications
        root = Root()
        root = DNSAdminAPI(
            save_queue=worker_manager.save_queue,
            delete_queue=worker_manager.delete_queue,
            backend_registry=registry,
        )
        root.health = HealthAPI(registry)

        # Add queue status endpoint
        root.queue_status = lambda: worker_manager.queue_status()

        cherrypy.tree.mount(root, "/")
        cherrypy.engine.start()
        logger.success(f"Server started on port {config.get_int('app.listen_port')}")

        # Add shutdown handler
        cherrypy.engine.subscribe("stop", worker_manager.stop)

        cherrypy.engine.block()

    except Exception as e:
        logger.critical(f"Server startup failed: {e}")
        if "worker_manager" in locals():
            worker_manager.stop()
        raise


if __name__ == "__main__":
    main()
