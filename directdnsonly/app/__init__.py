from loguru import logger
import sys
from directdnsonly.config import config


def configure_logging():
    logger.remove()
    level = config.get("log_level") or "INFO"
    json_mode = (config.get_string("log_format") or "text").lower() == "json"
    if json_mode:
        logger.add(sys.stderr, level=level, serialize=True)
    else:
        logger.add(
            sys.stderr,
            level=level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        )
    logger.add(
        "logs/directdnsonly_{time}.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
        serialize=json_mode,
    )
