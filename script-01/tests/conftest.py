import sys
from pathlib import Path

_here = Path(__file__).parent
# Allow importing script-01 modules, shared/, and tests/ helpers
sys.path.insert(0, str(_here))               # tests/helpers.py
sys.path.insert(0, str(_here.parent))        # script-01 modules
sys.path.insert(0, str(_here.parent.parent)) # shared/
