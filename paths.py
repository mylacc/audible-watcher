"""Where Audible Watcher keeps its files.

RESOURCE_DIR holds the code's own files (templates, static).
DATA_DIR holds your data: watcher.db and audible_auth.json. AW_DATA_DIR
wins if set (the Docker image sets /data); otherwise the folder app.py is
in.
"""
from __future__ import annotations

import os
import sys

RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get("AW_DATA_DIR") or RESOURCE_DIR
os.makedirs(DATA_DIR, exist_ok=True)


def check_data_dir_writable():
    """Stop with a clear message (not a database traceback) if the data
    folder can't be written, e.g. a Docker volume not owned by the
    container's user."""
    probe = os.path.join(DATA_DIR, ".write-test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        uid = os.getuid() if hasattr(os, "getuid") else None
        msg = (f"Audible Watcher can't write to its data folder {DATA_DIR} "
               f"({e.strerror}).")
        if uid is not None:
            msg += (f" It runs as user {uid}. On Docker, give that user the "
                    f"folder once: sudo chown -R {uid}:{uid} ./data")
        sys.stderr.write(msg + "\n")
        raise SystemExit(2)
