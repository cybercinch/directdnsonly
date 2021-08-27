import re

from sqlalchemy import create_engine


def connect(hostname, sid, username, password, port=1521) -> object:
    host = hostname
    port = port
    sid = sid
    user = username
    password = password
    sid = cx_Oracle.makedsn(host, port, sid=sid)

    connection_string = 'oracle://{user}:{password}@{sid}'.format(
        user=user,
        password=password,
        sid=sid
    )

    engine = create_engine(
        connection_string,
        convert_unicode=False,
        pool_recycle=10,
        pool_size=50,
    )

    return engine


def make_address(flt_num: str, street_name: str, area: str, address_string: str) -> str:
    """
        Take input parameters from GIS Data and returns Address as string
    :rtype: str
    """
    street_number = nv(flt_num).upper().strip()  # Make the street number upper case and strip whitespace
    street_name = nv(street_name).upper().strip()  # Make the street name upper case and strip whitespace
    town_name = nv(area)
    if area != 'None' and \
            area is not None and \
            area != '':
        town_name = nv(area).upper().strip()  # Make the area upper case and strip whitespace
    else:
        town_name = nv(lookup_town_in_string(address_string))  # Get the town from address string

    # Assemble the address string
    full_address = street_number + " " + street_name + " " + town_name

    return full_address


def lookup_town_in_string(address: str) -> str:
    for p in street_type_lookup():
        first_word = r"^(\w+)\s?((?!\\1)([\w]+)?)(?:\s+[\d]{4})"  # Return First Words
        try:
            f = address.index(p)
            size = len(p)
            if f is not None:
                m = re.search(first_word, address[f + size::].strip())
                if m.group(1) is not None and m.group(2) is not None:
                    if m.group(1) != m.group(2):
                        return m.group(1) + ' ' + m.group(2)
                    else:
                        return m.group(1)
                elif m.group(1) is not None and m.group(2) is None:
                    return m.group(1)
        except ValueError:
            pass
