import cherrypy
from cherrypy import request
from pythonjsonlogger import jsonlogger
from persistqueue import Queue, Empty
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import subprocess
import time
import sys
import yaml
import threading
import lib.common
import lib.db
import lib.db.models
import urllib.parse


class DaDNS(object):

    @cherrypy.expose
    def CMD_API_LOGIN_TEST(self):
        return urllib.parse.urlencode({'error': 0,
                                       'text': 'Login OK'})

    @cherrypy.expose
    def CMD_API_DNS_ADMIN(self):
        applog.debug('Processing Method: '.format(request.method))

        if request.method == 'POST':
            action = request.params.get('action')
            applog.debug('Action received via querystring: {}'.format(action))
            body = str(request.body.read(), 'utf-8')
            decoded_params = None
            if action is None:
                applog.debug('Action was not specified, check body')
                decoded_params = decode_params(str(body))
                applog.debug('Parameters decoded: {}'.format(decoded_params))
                action = decoded_params['action']
            zone_file = body
            applog.debug(zone_file)
            if action == 'delete':
                # TODO: Support multiple domain deletion
                # Domain is being removed from the DNS
                queue_item('delete', {'hostname': decoded_params['hostname'],
                                      'domain': decoded_params['select0']})
                return urllib.parse.urlencode({'error': 0})
            if action == 'rawsave':
                # DirectAdmin wants to add/update a domain
                queue_item('save', {'hostname': request.params.get('hostname'),
                                    'username': request.params.get('username'),
                                    'domain': request.params.get('domain'),
                                    'zone_file': zone_file})
                applog.info('Enqueued {} request for {}'.format('save', request.params.get('domain')))
                return urllib.parse.urlencode({'error': 0})
        elif request.method == 'GET':
            applog.debug('Action Type: ' + request.params.get('action'))
            action = request.params.get('action')
            check_parent = bool(request.params.get('check_for_parent_domain'))
            if action == 'exists' and check_parent:
                domain_result = check_zone_exists(request.params.get('domain'))
                applog.debug('Domain result: {}'.format(domain_result))
                parent_result = check_parent_domain_owner(request.params.get('domain'))
                applog.debug('Domain result: {}'.format(domain_result))
                if not domain_result and not parent_result:
                    return urllib.parse.urlencode({'error': 0,
                                                  'exists': 0})
                elif domain_result:
                    domain_record = session.query(lib.db.models.Domain).filter_by(
                        domain=request.params.get('domain')).one()
                    return urllib.parse.urlencode({'error': 0,
                                                   'exists': 1,
                                                   'details': 'Domain exists on {}'
                                                   .format(domain_record.hostname)
                                                   })
                elif parent_result:
                    parent_domain = ".".join(request.params.get('domain').split('.')[1:])
                    domain_record = session.query(lib.db.models.Domain).filter_by(
                        domain=parent_domain).one()
                    return urllib.parse.urlencode({'error': 0,
                                                   'exists': 2,
                                                   'details': 'Parent Domain exists on {}'
                                                   .format(domain_record.hostname)
                                                   })

            elif action == 'exists':
                # DirectAdmin is checking whether the domain is in the cluster
                if check_zone_exists(request.params.get('domain')):
                    domain_record = session.query(lib.db.models.Domain).filter_by(
                        domain=request.params.get('domain')).one()
                    return urllib.parse.urlencode({'error': 0,
                                                   'exists': 1,
                                                   'details': 'Domain exists on {}'
                                                   .format(domain_record.hostname)
                                                   })
                else:
                    return urllib.parse.urlencode({'exists': 0})


def put_zone_index(zone_name, host_name, user_name):
    # add a new zone to index
    applog.debug('Placed zone into database.. {}'.format(str(zone_name)))
    domain = lib.db.models.Domain(domain=zone_name, hostname=host_name, username=user_name)
    session.add(domain)
    session.commit()


def queue_item(action, data=None):
    data = {'payload': data}
    if action == 'save':
        save_queue.put(data)
    elif action == 'delete':
        delete_queue.put(data)


def delete_zone_file(zone_name):
    # Delete the zone file
    applog.debug('Zone Name for delete: ' + zone_name)
    os.remove(zones_dir + '/' + zone_name + '.db')
    applog.debug('Zone deleted: {}'.format(zones_dir + '/' + zone_name + '.db'))


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
            f.write('zone "' + domain.domain
                    + '" { type master; file "' + zones_dir + '/'
                    + domain.domain + '.db"; };\n')


def check_parent_domain_owner(zone_name):
    applog.debug('Checking if {} exists in the DB'.format(zone_name))
    # check try to find domain name
    parent_domain = ".".join(zone_name.split('.')[1:])
    domain_exists = session.query(session.query(lib.db.models.Domain).filter_by(domain=parent_domain).exists()).scalar()
    if domain_exists:
        # domain exists in the db
        applog.debug('{} exists in db'.format(parent_domain))
        domain_record = session.query(lib.db.models.Domain).filter_by(domain=parent_domain).one()
        applog.debug(str(domain_record))
        return True
    else:
        return False


def reconfigure_nameserver():
    env = dict(os.environ)  # make a copy of the environment
    lp_key = 'LD_LIBRARY_PATH'  # for Linux and *BSD
    lp_orig = env.get(lp_key + '_ORIG')  # pyinstaller >= 20160820
    if lp_orig is not None:
        env[lp_key] = lp_orig  # restore the original
    else:
        env.pop(lp_key, None)  # last resort: remove the env var

    reconfigure = subprocess.run(['rndc', 'reconfig'],
                                 capture_output=True,
                                 universal_newlines=True,
                                 env=env)
    applog.debug("Stdout: {}".format(reconfigure.stdout))
    applog.info('Reloaded bind')


def reload_nameserver(zone=None):
    # Workaround for LD_LIBRARY_PATH/ LIBPATH issues
    #
    env = dict(os.environ)  # make a copy of the environment
    lp_key = 'LD_LIBRARY_PATH'  # for Linux and *BSD
    lp_orig = env.get(lp_key + '_ORIG')  # pyinstaller >= 20160820
    if lp_orig is not None:
        env[lp_key] = lp_orig  # restore the original
    else:
        env.pop(lp_key, None)  # last resort: remove the env var

    if zone is not None:
        reload = subprocess.run(['rndc', 'reload', zone],
                                capture_output=True,
                                universal_newlines=True,
                                env=env)
        applog.debug("Stdout: {}".format(reload.stdout))
        applog.info('Reloaded bind for {}'.format(zone))
    else:
        reload = subprocess.run(['rndc', 'reload'],
                                capture_output=True,
                                universal_newlines=True,
                                env=env)
        applog.debug("Stdout: {}".format(reload.stdout))
        applog.info('Reloaded bind')


def check_zone_exists(zone_name):
    # Check if zone is present in the index
    applog.debug('Checking if {} is present in the DB'.format(zone_name))
    domain_exists = bool(session.query(lib.db.models.Domain.id).filter_by(domain=zone_name).first())
    applog.debug('Returned from query: {}'.format(domain_exists))
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


def background_thread(worker_type):
    if worker_type == 'save':
        applog.debug('Started worker thread for save action')
        while True:
            try:
                item = save_queue.get(block=True, timeout=10)
                data = item['payload']
                applog.info('Processing save from queue for {}'.format(data['domain']))
                applog.debug('Domain name to check: ' + data['domain'])
                applog.debug('Does zone exist? ' + str(check_zone_exists(str(data['domain']))))
                if not check_zone_exists(str(data['domain'])):
                    applog.debug('Zone is not present in db')
                    put_zone_index(str(data['domain']), str(data['hostname']), str(data['username']))
                    write_zone_file(str(data['domain']), data['zone_file'])
                    write_named_include()
                    reconfigure_nameserver()
                    reload_nameserver(str(data['domain']))
                else:
                    # Domain already exists
                    applog.debug('Zone is present in db')
                    write_zone_file(str(data['domain']), data['zone_file'])
                    write_named_include()
                    reload_nameserver(str(data['domain']))
                save_queue.task_done()

            except Empty:
                # Queue is empty
                applog.debug('Save queue is empty')
    elif worker_type == 'delete':
        applog.debug('Started worker thread for delete action')
        while True:
            try:
                item = delete_queue.get(block=True, timeout=10)
                data = item['payload']
                applog.info('Processing deletion from queue for {}'.format(data['domain']))
                record = session.query(lib.db.models.Domain).filter_by(domain=data['domain']).one()
                if record.hostname == data['hostname']:
                    applog.debug('Hostname matches the original host {}: Delete is allowed'.format(data['domain']))
                    session.delete(record)
                    session.commit()
                    applog.info('{} deleted from database'.format(data['domain']))
                    delete_zone_file(data['domain'])
                    write_named_include()
                    reload_nameserver()
                delete_queue.task_done()
                time.sleep(5)
            except Empty:
                # Queue is empty
                applog.debug('Delete queue is empty')
            except Exception as e:
                applog.error(e)


def setup_logging():
    os.environ['TZ'] = config['timezone']
    time.tzset()
    _applog = logging.getLogger()
    _applog.setLevel(level=getattr(logging, config['log_level'].upper()))
    if config['log_to'] == 'stdout':
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level=getattr(logging, config['log_level'].upper()))
        formatter = jsonlogger.JsonFormatter(
            fmt='%(asctime)s %(levelname)s %(message)s'
        )
        handler.setFormatter(formatter)
        _applog.addHandler(handler)
    elif config['log_to'] == 'file':
        handler = TimedRotatingFileHandler(config['log_path'],
                                           when='midnight',
                                           backupCount=10)
        handler.setLevel(level=getattr(logging, config['log_level'].upper()))
        formatter = jsonlogger.JsonFormatter(
            fmt='%(asctime)s %(levelname)s %(message)s'
        )
        handler.setFormatter(formatter)
        _applog.addHandler(handler)
    return _applog


if __name__ == '__main__':
    app_version = "1.0.9"
    if os.path.isfile("/lib/x86_64-linux-gnu/" + "libgcc_s.so.1"):
        # Load local library
        libgcc_s = ctypes.cdll.LoadLibrary("/lib/x86_64-linux-gnu/" + "libgcc_s.so.1")
    # We are about to start our application
    with open(r'conf/app.yml') as config_file:
        config = yaml.load(config_file, Loader=yaml.SafeLoader)
    applog = setup_logging()
    applog.info('DirectDNS Starting')
    applog.info('Timezone is {}'.format(config['timezone']))
    applog.info('Get Database Connection')
    session = lib.db.connect(config['db_location'])
    applog.info('Database Connected!')

    zones_dir = "/etc/named/directdnsonly"
    named_conf = "/etc/named/directdnsonly.inc"

    save_queue = Queue(config['queue_location'] + '/rawsave')
    save_thread = threading.Thread(target=background_thread, args=('save',))
    save_thread.daemon = True  # Daemonize thread
    save_thread.start()  # Start the execution
    delete_queue = Queue(config['queue_location'] + '/delete')
    delete_thread = threading.Thread(target=background_thread, args=('delete',))
    delete_thread.daemon = True  # Daemonize thread
    delete_thread.start()  # Start the execution

    cherrypy.__version__ = ''
    cherrypy._cperror._HTTPErrorTemplate = cherrypy._cperror._HTTPErrorTemplate.replace(
        'Powered by <a href="http://www.cherrypy.org">CherryPy %(version)s</a>\n', '%(version)s')

    user_password_dict = {'test': 'test'}
    check_password = cherrypy.lib.auth_basic.checkpassword_dict(user_password_dict)

    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': config['server_port'],
        'tools.proxy.on': config['proxy_support'],
        'tools.proxy.base': config['proxy_support_base'],
        'tools.auth_basic.on': True,
        'tools.auth_basic.realm': 'dadns',
        'tools.auth_basic.checkpassword': check_password,
        'tools.response_headers.on': True,
        'tools.response_headers.headers': [('Server', 'DirectDNS v' + app_version)],
        'environment': config['environment']
    })

    if bool(config['ssl_enable']):
        cherrypy.config.update({
            'server.ssl_module': 'builtin',
            'server.ssl_certificate': config['ssl_cert'],
            'server.ssl_private_key': config['ssl_key'],
            'server.ssl_certificate_chain': config['ssl_bundle']
        })

    # cherrypy.log.error_log.propagate = False
    if config['log_level'].upper() != 'DEBUG':
        cherrypy.log.access_log.propagate = False

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
