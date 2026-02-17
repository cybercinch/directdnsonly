#!/usr/bin/env just --justfile
APP_NAME := "directdnsonly"
build:
    cd src && \
    pyinstaller \
    -p . \
    --hidden-import=json \
    --hidden-import=pyopenssl \
    --hidden-import=pymysql \
    --hidden-import=jaraco \
    --hidden-import=cheroot \
    --hidden-import=cheroot.ssl.pyopenssl \
    --hidden-import=cheroot.ssl.builtin \
    --hidden-import=lib \
    --hidden-import=os \
    --hidden-import=builtins \
    --noconfirm --onefile {{APP_NAME}}.py