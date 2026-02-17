from loguru import logger
import sys
from directdnsonly.config import config


def configure_logging():
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.get("log_level"),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )
    logger.add(
        "logs/directdnsonly_{time}.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
    )
