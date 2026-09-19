"""Entry point: ``python -m davideo``."""

from __future__ import annotations

import sys

from .core.credentials import CredentialError, get_credential_store
from .core.store import ConfigStore


def main() -> int:
    try:
        creds = get_credential_store()
    except CredentialError as exc:
        print(f"credential store unavailable:\n  {exc}", file=sys.stderr)
        return 1

    store = ConfigStore()

    # import late so `python -m davideo --help`-style checks don't need textual
    from .tui.app import WebDavTuiApp

    WebDavTuiApp(store, creds).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
