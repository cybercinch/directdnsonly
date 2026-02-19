from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from vyper import v
from loguru import logger

import datetime

Base = declarative_base()


def _migrate(engine):
    """Apply additive schema migrations for columns added after initial release."""
    migrations = [
        ("domains", "zone_data", "ALTER TABLE domains ADD COLUMN zone_data TEXT"),
        (
            "domains",
            "zone_updated_at",
            "ALTER TABLE domains ADD COLUMN zone_updated_at DATETIME",
        ),
    ]
    with engine.connect() as conn:
        for table, column, ddl in migrations:
            try:
                conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                try:
                    conn.execute(text(ddl))
                    conn.commit()
                    logger.info(f"[db] Migration applied: added {table}.{column}")
                except Exception as exc:
                    logger.warning(f"[db] Migration skipped ({table}.{column}): {exc}")


def connect(dbtype="sqlite", **kwargs):
    if dbtype == "sqlite":
        # Start SQLite engine
        db_location = v.get("datastore.db_location")
        if db_location == -1:
            raise Exception("DB Type is sqlite but db_location is not defined")
        else:
            engine = create_engine(
                "sqlite:///" + db_location, connect_args={"check_same_thread": False}
            )
            Base.metadata.create_all(engine)
            _migrate(engine)
            return sessionmaker(engine)()
    elif dbtype == "mysql":
        # Start a MySQL engine
        db_user = v.get_string("datastore.user")
        db_host = v.get_string("datastore.host")
        db_name = v.get_string("datastore.name")
        db_pass = v.get_string("datastore.pass")
        db_port = v.get_string("datastore.port")
        if (
            not v.is_set("datastore.user")
            or not v.is_set("datastore.name")
            or not v.is_set("datastore.pass")
            or not v.is_set("datastore.host")
        ):
            raise Exception(
                "DB Type is mysql but db_(host,name,and pass) are not populated"
            )
        else:
            engine = create_engine(
                "mysql+pymysql://"
                + db_user
                + ":"
                + db_pass
                + "@"
                + db_host
                + ":"
                + db_port
                + "/"
                + db_name
            )
            Base.metadata.create_all(engine)
            _migrate(engine)
            return sessionmaker(engine)()
    else:
        raise Exception("Unknown/unimplemented database type: {}".format(dbtype))
