"""Local on/off lease. No network, credentials, model calls, or settings rewrites."""
import json
from pathlib import Path
import time


def read(path):
    try:
        value = json.loads(Path(path).read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def allowed(home, *, now=None):
    root = Path(home) / 'interior-remote'
    if not (root / 'config.json').exists():
        return True
    lease = read(root / 'lease.json')
    moment = time.time() if now is None else now
    expiry = lease.get('expires_at')
    return (lease.get('enabled') is True and isinstance(expiry, (int, float))
            and 0 < expiry - moment <= 120)


def require(home):
    if not allowed(home):
        raise AutomationPaused('The website paused corrections or its connection needs to return.')


class AutomationPaused(Exception):
    """Finish an open document locally; retain its batch and immutable result."""
