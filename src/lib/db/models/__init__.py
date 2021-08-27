from lib.db import Base
from sqlalchemy import Column, Integer, String, DateTime


class Key(Base):
    __tablename__ = "keys"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True)
    name = Column(String)
    expires = Column(DateTime)
    service = Column(String)

    def __repr__(self):
        return "<Key(key='%s', name='%s', expires='%s', service='%s')>" % (
            self.key, self.name, self.expires, self.service)


class Domain(Base):
    __tablename__ = "domains"
    id = Column(Integer, primary_key=True)
    domain = Column(String, unique=True)
    hostname = Column(String)
    username = Column(String)

    def __repr__(self):
        return "<Domain(id='%s', domain='%s', hostname='%s', username='%s')>" % (
            self.id, self.domain, self.hostname, self.username
        )
