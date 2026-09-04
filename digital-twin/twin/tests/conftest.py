"""Pytest configuration: ensure tests can import `twin.*` regardless of cwd."""
import os
import sys

# Insert the digital-twin/ root on sys.path so `import twin.*`
# works when pytest is invoked from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
TWIN_PKG_PARENT = os.path.dirname(os.path.dirname(HERE))  # .../digital-twin
if TWIN_PKG_PARENT not in sys.path:
    sys.path.insert(0, TWIN_PKG_PARENT)
