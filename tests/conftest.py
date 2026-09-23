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

    # Mirror the real inheritance tree, not just the names. `_is_transient`
    # classifies by isinstance, so a stub with flat sibling classes would hide
    # exactly the bug these tests exist to catch: ChunkedEncodingError is a
    # RequestException but is NOT a ConnectionError.
    class _RequestException(OSError):
        pass

    requests.RequestException = _RequestException
    requests.ConnectionError = type("ConnectionError", (_RequestException,), {})
    requests.Timeout = type("Timeout", (_RequestException,), {})
    requests.HTTPError = type("HTTPError", (_RequestException,), {})
    requests.ChunkedEncodingError = type("ChunkedEncodingError", (_RequestException,), {})
    requests.Session = type("Session", (), {})

    exceptions = types.ModuleType("requests.exceptions")
    for _name in ("RequestException", "ConnectionError", "Timeout", "HTTPError",
                  "ChunkedEncodingError"):
        setattr(exceptions, _name, getattr(requests, _name))
    exceptions.ContentDecodingError = type(
        "ContentDecodingError", (_RequestException,), {}
    )
    requests.exceptions = exceptions
    sys.modules["requests"] = requests
    sys.modules["requests.exceptions"] = exceptions

if "bs4" not in sys.modules:
    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = object
    sys.modules["bs4"] = bs4

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv


def fake_config(tmp_path=None, **overrides):
    """A Config stand-in carrying every field the server/worker paths read.

    Each call site used to build its own bare ``SimpleNamespace``, so adding a
    Config field broke a dozen unrelated tests — and, worse, a fake missing a
    field silently modelled a configuration that cannot exist. One factory
    keeps the fakes in step with the real dataclass.
    """
    import types as _types
    from pathlib import Path as _Path

    base = {
        "cache_dir": (tmp_path / "cache") if tmp_path is not None else _Path("/nonexistent"),
        "nomatch_ttl_days": 30,
        "history_size": 50,
    }
    base.update(overrides)
    return _types.SimpleNamespace(**base)
