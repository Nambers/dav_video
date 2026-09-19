"""davideo: a terminal WebDAV browser that hands media to mpv.

Layering (important for future port to a faster language):
  core/  -- pure business logic, ZERO ui dependency. Port target.
  tui/   -- Textual front-end, disposable.
"""

# The one version string: pyproject.toml reads it from here.
__version__ = "0.1.0"
# Shown in the about box (I).
PROJECT_URL = "https://github.com/nambers/davideo"
