# conftest.py — adds the project root to sys.path so that `pytest tests/`
# works from the repo root without needing an installed package.
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
