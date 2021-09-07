from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import datetime

Base = declarative_base()


def connect(db_location):
    # Start SQLite engine
    engine = create_engine('sqlite:///' + db_location, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    return session