"""Compatibility import for the shared mission helpers.

Run from the repository, or use bundle_bridge.py for a standalone Pi directory.
"""
from pathlib import Path
import sys

try:
    from yp_common import sar_missions as _implementation
except ModuleNotFoundError as exc:
    if exc.name != "yp_common":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from yp_common import sar_missions as _implementation

sys.modules[__name__] = _implementation
