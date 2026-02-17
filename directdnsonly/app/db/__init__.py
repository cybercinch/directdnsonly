from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from vyper import v

import datetime

Base = declarative_base()


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
            return sessionmaker(bind=engine)()
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
            return sessionmaker(bind=engine)()
    else:
        raise Exception("Unknown/unimplemented database type: {}".format(dbtype))
