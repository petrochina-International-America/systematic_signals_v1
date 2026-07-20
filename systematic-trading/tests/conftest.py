import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)           # systematic-trading/
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)          # h:\SystematicTrading\

for _p in (_PROJECT_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
