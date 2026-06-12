"""Shared test setup: keep favourites DB writes out of the repo working dir."""
import os
import tempfile

# Must be set before `favourites` is imported (it binds DB_PATH at import time).
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "nusbot_test_fav.db"))
