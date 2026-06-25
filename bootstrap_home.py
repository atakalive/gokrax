"""HOME normalization — pin gokrax state paths to the login-user HOME.

Imported by gokrax.py / watchdog.py *before* ``config``. Importing this module
runs ``normalize_home()`` as a side-effect so that every ``Path.home()`` /
``expanduser("~")`` derived path resolves against the real login user's HOME
instead of a drifted ``$HOME`` (e.g. an isolated agent HOME). stdlib only; must
NOT import ``config`` (import-order safety).
"""

from __future__ import annotations

import os


def real_login_home() -> str | None:
    """Return the login user's HOME from passwd (uid -> home), or None.

    Uses passwd rather than ``$HOME`` so a polluted ``$HOME`` cannot affect the
    result. Returns None on non-POSIX platforms (no ``pwd`` module) or if the
    lookup fails for any reason.
    """
    try:
        import pwd
    except ImportError:
        return None
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError, AttributeError):
        return None
    return home or None


def foreign_home_allowed() -> bool:
    """True when GOKRAX_ALLOW_FOREIGN_HOME opts out of normalization.

    Truthiness follows the GOKRAX_DRY_RUN convention (config/__init__.py):
    the value is .strip()ped and anything other than "", "0", "false" is truthy.
    """
    return os.environ.get("GOKRAX_ALLOW_FOREIGN_HOME", "").strip() not in ("", "0", "false")


def normalize_home() -> None:
    """Pin ``os.environ['HOME']`` to the login user's passwd home. Idempotent.

    No-op when the escape valve is set, when passwd home is unavailable
    (non-POSIX / lookup failure), when it does not exist on disk, or when
    ``$HOME`` already matches.
    """
    if foreign_home_allowed():
        return
    home = real_login_home()
    if home and os.path.isdir(home) and os.environ.get("HOME") != home:
        os.environ["HOME"] = home


normalize_home()
