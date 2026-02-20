import os
import sys


def run():
    # main.py uses short-form imports (from app.*, from worker) that resolve
    # relative to the directdnsonly/ package directory. Insert it into the
    # path before importing so `python -m directdnsonly` and the `dadns`
    # console script both work without changing main.py.
    sys.path.insert(0, os.path.dirname(__file__))
    from main import main

    main()


if __name__ == "__main__":
    run()
