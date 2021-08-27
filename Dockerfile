FROM pypy:slim-buster

RUN mkdir -p /opt/apikeyhandler/config
VOLUME /opt/apikeyhandler/config

COPY ./src/ /opt/apikeyhandler
WORKDIR /opt/apikeyhandler

RUN pip install -r requirements.txt

CMD pypy3 main.py