"""Test bootstrap.

``describarr.workflow`` pulls in ``describarr.audiovault`` (and ``config``),
which import ``requests``, ``bs4`` and ``python-dotenv`` at module load. The
acceptance-gate and backup logic under test touch none of them, so we inject
minimal stubs to keep these unit tests runnable without a network stack
installed.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "requests" not in sys.modules:
    requests = types.ModuleType("requests")

    class _Err(Exception):
        pass

    requests.ConnectionError = type("ConnectionError", (_Err,), {})
    requests.Timeout = type("Timeout", (_Err,), {})
    requests.HTTPError = type("HTTPError", (_Err,), {})
    requests.Session = type("Session", (), {})
    sys.modules["requests"] = requests

if "bs4" not in sys.modules:
    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = object
    sys.modules["bs4"] = bs4

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv
