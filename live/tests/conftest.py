"""Pytest configuration: ensure tests can import `live.*` regardless of cwd."""
import os
import sys

# Insert the repo root (parent of `live/`) on sys.path so `import live.*`
# works when pytest is invoked from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
