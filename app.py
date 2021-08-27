from flask import Flask, request
import mmap
import re

app = Flask(__name__)


@app.route('/')
def hello_world():
    return 'Hello World!'


@app.route('/CMD_API_LOGIN_TEST')
def login_test():
    multi_dict = request.values
    for key in multi_dict:
        print(multi_dict.get(key))
        print(multi_dict.getlist(key))
    # print(request.values)
    print(request.headers)
    print(request.authorization)

    return 'error=0&text=Login OK&details=none'


@app.route('/CMD_API_DNS_ADMIN', methods=['GET', 'POST'])
def domain_admin():
    print(str(request.data, encoding="utf-8"))
    print(request.values.get('action'))
    action = request.values.get('action')
    if action == 'exists':
        # DirectAdmin is checking whether the domain is in the cluster
        return 'result: exists=1'
    if action == 'delete':
        # Domain is being removed from the DNS
        hostname = request.values.get('hostname')
        username = request.values.get('username')
        domain = request.values.get('select0')


    if action == 'rawsave':
        # DirectAdmin wants to add/update a domain
        hostname = request.values.get('hostname')
        username = request.values.get('username')
        domain = request.values.get('domain')

        if not check_zone_exists(str(domain)):
            put_zone_index(str(domain))
            write_zone_file(str(domain), request.data.decode("utf-8"))
        else:
            # Domain already exists
            write_zone_file(str(domain), request.data.decode("utf-8"))


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


def put_zone_index(zone_name):
    # add a new zone to index
    with open(zone_index_file, 'a+') as f:
        # We are using append mode
        f.write(zone_name)


def write_zone_file(zone_name, data):
    # Write the zone to file
    with open(zones_dir + '/' + zone_name + '.db', 'w') as f:
        f.write(data)


def check_zone_exists(zone_name):
    # Check if zone is present in the index
    with open(zone_index_file, 'r') as f:
        try:
            s = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            if s.find(bytes(zone_name, encoding='utf8')) != -1:
                return True
            else:
                return False
        except ValueError as e:
            # File Empty?
            return False


if __name__ == '__main__':
    zones_dir = "/etc/pdns/zones"
    zone_index_file = "/etc/pdns/zones/.index"
    named_conf = "/etc/pdns/named.conf"
    create_zone_index()

    app.run(host="0.0.0.0")
