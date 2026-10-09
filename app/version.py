"""The app's release version: the single source of truth (SemVer, no leading "v").

Bump this together with CHANGELOG.md in the PR that makes the change; see docs/RELEASING.md.
The git tag for a release is "v" + __version__ (e.g. v0.3.0). pyproject.toml reads its version
from here, /health reports it and every page footer shows it.
"""

__version__ = "0.5.1"
