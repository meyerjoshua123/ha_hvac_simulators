"""Load the pure-Python modules without importing Home Assistant."""

import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "hvac_simulators"

# Register a bare package so ``hvac_simulators.engine`` etc. import without
# running ``__init__.py`` (which needs Home Assistant).
if "hvac_simulators" not in sys.modules:
    pkg = types.ModuleType("hvac_simulators")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["hvac_simulators"] = pkg
