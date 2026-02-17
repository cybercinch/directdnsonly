from loguru import logger

from directdnsonly.app.db.models import *
from directdnsonly.app.db import connect


def check_zone_exists(zone_name):
    # Check if zone is present in the index
    session = connect()
    logger.debug("Checking if {} is present in the DB".format(zone_name))
    domain_exists = bool(session.query(Domain.id).filter_by(domain=zone_name).first())
    logger.debug("Returned from query: {}".format(domain_exists))
    if domain_exists:
        return True
    else:
        return False


def put_zone_index(zone_name, host_name, user_name):
    # add a new zone to index
    session = connect()
    logger.debug("Placed zone into database.. {}".format(str(zone_name)))
    domain = Domain(domain=zone_name, hostname=host_name, username=user_name)
    session.add(domain)
    session.commit()


def get_domain_record(zone_name):
    """Return the Domain record for zone_name, or None if not found"""
    session = connect()
    return session.query(Domain).filter_by(domain=zone_name).first()


def check_parent_domain_owner(zone_name):
    """Return True if the immediate parent domain of zone_name exists in the DB"""
    parent_domain = ".".join(zone_name.split(".")[1:])
    if not parent_domain:
        return False
    session = connect()
    logger.debug("Checking if parent domain {} exists in DB".format(parent_domain))
    return bool(session.query(Domain.id).filter_by(domain=parent_domain).first())


def get_parent_domain_record(zone_name):
    """Return the Domain record for the parent of zone_name, or None"""
    parent_domain = ".".join(zone_name.split(".")[1:])
    if not parent_domain:
        return None
    session = connect()
    return session.query(Domain).filter_by(domain=parent_domain).first()
