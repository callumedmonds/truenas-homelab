"""Site-specific values (drive serials, addresses) loaded from homelab.env.

The real values live in homelab.env next to these scripts, which is gitignored:
serials and addresses identify one specific machine and have no business in a
public repository. homelab.env.example documents every key.

Import works without any path setup because python3 puts a script's own
directory first on sys.path, and the launchers deploy scripts/ as a directory.
"""

import os
import sys

_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homelab.env")
_cache = None


def load(path=None):
    """Parse homelab.env into a dict. Cached; the environment always wins."""
    global _cache
    if path is None and _cache is not None:
        return _cache

    target = path or os.environ.get("HOMELAB_ENV") or _DEFAULT
    conf = {}
    try:
        with open(target) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        sys.exit(
            "%s not found.\n"
            "Copy scripts/homelab.env.example to homelab.env beside the scripts "
            "and fill in your own serials and addresses." % target
        )

    if path is None:
        _cache = conf
    return conf


def get(key, default=None):
    value = os.environ.get(key) or load().get(key, default)
    if value is None:
        sys.exit("%s is not set -- add it to homelab.env (see homelab.env.example)" % key)
    return value


def pairs(key):
    """Parse "A:one,B:two" into {"A": "one", "B": "two"}, preserving order."""
    out = {}
    for item in get(key, "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, label = item.partition(":")
        out[name.strip()] = label.strip()
    if not out:
        sys.exit("%s is empty -- add it to homelab.env (see homelab.env.example)" % key)
    return out
