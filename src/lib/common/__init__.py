import random
import string
import lib.db.models


def check_if_super_user_exists(session):
    exists = session.query(session.query(lib.db.models.Key).filter_by(name='super').exists()).scalar()
    return exists


def check_if_domain_exists(session):
    pass


def get_random_string(length):
    letters_and_digits = string.ascii_letters + string.digits
    result_str = ''.join(random.choice(letters_and_digits) for i in range(length))
    return result_str