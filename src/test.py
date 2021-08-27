import datetime
import lib.common
import lib.db
import lib.db.models

new_expiry_date = datetime.datetime.now() + datetime.timedelta(int(10))

session = lib.db.connect()

if not lib.common.check_if_super_user_exists(session):
    password_str = lib.common.get_random_string(20)
    print('Creating superuser account: {}'.format('super'))
    print('Password: {}'.format(password_str))
    super = lib.db.models.Key(key=password_str, name='super', service='*')
    session.add(super)
    session.commit()
else:
    print('Superuser account already exists: skipping creation')