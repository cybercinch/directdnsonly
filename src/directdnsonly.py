import mmap

import cherrypy
from cherrypy import request
from cherrypy._cpnative_server import CPHTTPServer
from pythonjsonlogger import jsonlogger
import logging
import os
import time
import sys
import yaml
import datetime
import lib.common
import lib.db
import lib.db.models


class DaDNS(object):

    @cherrypy.expose
    def CMD_API_LOGIN_TEST(self, **params):
        return 'error=0&text=Login OK&details=none'

    @cherrypy.expose
    def CMD_API_DNS_ADMIN(self, **params):
        applog.debug('Processing Method: ' + request.method)

        if request.method == 'POST':
            action = request.params.get('action')
            decoded_params = None
            if action is None:
                decoded_params = decode_params(str(request.body.read(), 'utf-8'))
                action = decoded_params['action']
            zone_file = str(request.body.read(), 'utf-8')
            applog.debug(zone_file)
            if action == 'delete':
                # Domain is being removed from the DNS
                hostname = decoded_params['hostname']
                domain = decoded_params['select0']
                record = session.query(lib.db.models.Domain).filter_by(domain=domain).one()
                if record.hostname == hostname:
                    applog.debug('Hostname matches the original host {}: Delete is allowed'.format('hostname'))
                    session.delete(record)
                    applog.info('{} deleted from database')
                    write_named_include()
            if action == 'rawsave':
                # DirectAdmin wants to add/update a domain
                hostname = request.params.get('hostname')
                username = request.params.get('username')
                domain = request.params.get('domain')
                applog.debug('Domain name to check: ' + domain)
                applog.debug('Does zone exist? ' + str(check_zone_exists(str(domain))))
                if not check_zone_exists(str(domain)):
                    applog.debug('Zone is not present in db')
                    put_zone_index(str(domain), str(hostname), str(username))
                    write_zone_file(str(domain), zone_file)
                else:
                    # Domain already exists
                    applog.debug('Zone is present in db')
                    write_zone_file(str(domain), zone_file)
        elif request.method == 'GET':
            applog.debug('Action Type: ' + request.params.get('action'))
            action = request.params.get('action')
            if action == 'exists':
                # DirectAdmin is checking whether the domain is in the cluster
                if check_zone_exists(request.params.get('domain')):
                    return 'result: exists=1'
                else:
                    return 'result: exists=0'


def create_zone_index():
    # Create an index of all zones present from zone definitions
    regex = r"(?<=\")(?P<domain>.*)(?=\"\s)"

    with open(zone_index_file, 'w+') as f:
        with open(named_conf, 'r') as named_file:
            while True:
                # read line
                line = named_file.readline()
                if not line:
                    # Reached end of file
                    break
                print(line)
                hosted_domain = re.search(regex, line).group(0)
                f.write(hosted_domain + "\n")


def put_zone_index(zone_name, host_name, user_name):
    # add a new zone to index
    applog.debug('Placed zone into database.. {}'.format(str(zone_name)))
    domain = lib.db.models.Domain(domain=zone_name, hostname=host_name, username=user_name)
    session.add(domain)
    session.commit()


def write_zone_file(zone_name, data):
    # Write the zone to file
    applog.debug('Zone Name for write: ' + zone_name)
    applog.debug('Zone file to write: \n' + data)
    with open(zones_dir + '/' + zone_name + '.db', 'w') as f:
        f.write(data)
    applog.debug('Zone written to {}'.format(zones_dir + '/' + zone_name + '.db'))


def write_named_include():
    applog.debug('Rewrite named zone include...')
    domains = session.query(lib.db.models.Domain).all()
    with open(named_conf, 'w') as f:
        for domain in domains:
            applog.debug('Writing zone {} to named.config'.format(domain.domain))
            f.write('zone "{}" 	{ type master; file "/etc/pdns/zones/{}.db"; };'
                    .format(domain.domain,
                            domain.domain))


def check_parent_domain_owner(zone_name, owner):
    applog.debug('Checking if {} is owner of parent in the DB'.format(zone_name))
    # check try to find domain name
    parent_domain = ".".join(zone_name.split('.')[1:])
    domain_exists = session.query(session.query(lib.db.models.Domain).filter_by(domain=parent_domain).exists()).scalar()
    if domain_exists:
        # domain exists in the db
        applog.debug('{} exists in db'.format(parent_domain))
        domain_record = session.query(lib.db.models.Domain).filter_by(domain=parent_domain).one()
        applog.debug(str(domain_record))
        if domain_record.username == owner:
            return True
        else:
            return False


def check_zone_exists(zone_name):
    # Check if zone is present in the index
    applog.debug('Checking if {} is present in the DB'.format(zone_name))
    domain_exists = session.query(session.query(lib.db.models.Domain).filter_by(domain=zone_name).exists()).scalar()
    if domain_exists:
        return True
    else:
        return False


def decode_params(payload):
    from urllib.parse import parse_qs
    response = parse_qs(payload)
    params = dict()
    for key, val in response.items():
        params[key] = val[0]
    return params


@cherrypy.expose
@cherrypy.tools.json_out()
def health(self):
    # Defaults to 200
    return {"Message": "OK!"}


def setup_logging():
    os.environ['TZ'] = config['timezone']
    time.tzset()
    applog = logging.getLogger()
    applog.setLevel(level=getattr(logging, config['log_level'].upper()))
    if config['log_to'] == 'stdout':
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level=getattr(logging, config['log_level'].upper()))
        formatter = jsonlogger.JsonFormatter(
            fmt='%(asctime)s %(levelname)s %(message)s'
        )
        handler.setFormatter(formatter)
        applog.addHandler(handler)
    elif config['log_to'] == 'file':
        handler = logging.FileHandler('./config/directdns.log')
        handler.setLevel(level=getattr(logging, config['log_level'].upper()))
        formatter = jsonlogger.JsonFormatter(
            fmt='%(asctime)s %(levelname)s %(message)s'
        )
        handler.setFormatter(formatter)
        applog.addHandler(handler)
    return applog


if __name__ == '__main__':
    app_version = "1.0.0"
    if os.path.isfile("/lib/x86_64-linux-gnu/" + "libgcc_s.so.1"):
        # Load local library
        libgcc_s = ctypes.cdll.LoadLibrary("/lib/x86_64-linux-gnu/" + "libgcc_s.so.1")
    # We are about to start our application
    with open(r'config/app.yml') as config_file:
        config = yaml.load(config_file, Loader=yaml.SafeLoader)
    applog = setup_logging()
    applog.info('DirectDNS Starting')
    applog.info('Timezone is {}'.format(config['timezone']))
    applog.info('Get Database Connection')
    session = lib.db.connect()
    applog.info('Database Connected!')

    zones_dir = "/etc/pdns/zones"
    named_conf = "/etc/pdns/named.conf"

    cherrypy.__version__ = ''
    cherrypy._cperror._HTTPErrorTemplate = cherrypy._cperror._HTTPErrorTemplate.replace(
        'Powered by <a href="http://www.cherrypy.org">CherryPy %(version)s</a>\n', '%(version)s')
    userpassdict = {'test': 'test'}
    checkpassword = cherrypy.lib.auth_basic.checkpassword_dict(userpassdict)

    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': config['server_port'],
        'tools.proxy.on': config['proxy_support'],
        'tools.proxy.base': config['proxy_support_base'],
        'tools.auth_basic.on': True,
        'tools.auth_basic.realm': 'dadns',
        'tools.auth_basic.checkpassword': checkpassword,
        'tools.response_headers.on': True,
        'tools.response_headers.headers': [('Server', 'DirectDNS v' + app_version)],
        'environment': config['environment']
    })
    # cherrypy.log.error_log.propagate = False
    # cherrypy.log.access_log.propagate = False

    if not lib.common.check_if_super_user_exists(session):
        password_str = lib.common.get_random_string(35)
        applog.info('Creating superuser account: {}'.format('super'))
        applog.info('Password: {}'.format(password_str))
        superuser = lib.db.models.Key(key=password_str, name='super', service='*')
        session.add(superuser)
        session.commit()
    else:
        applog.info('Superuser account already exists: skipping creation')

    cherrypy.quickstart(DaDNS())
