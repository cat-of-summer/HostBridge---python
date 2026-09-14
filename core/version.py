r"""Version and schema constants.

This is where the version lives, and the only place. ``pyproject.toml`` declares it dynamic
and reads the line below; ``build/version_stamp.py`` rewrites that line from the release tag
before anything is collected. So the tag, the package metadata and the binary's
``--version`` are one fact with one source, rather than three literals somebody has to
remember to keep equal.
"""

from __future__ import annotations

#: Rewritten by ``build/version_stamp.py`` on a tagged build. ``0.0.0`` is what a build that
#: was not cut from a release tag honestly is -- a branch push, a pull request, a developer's
#: local run -- and saying so beats inheriting whatever number the last release happened to
#: leave behind in the file.
__version__ = "0.0.0"

#: On-disk layout of ``domains.json``. Bumped whenever ``core.model.Domain`` gains or
#: loses a persisted field; ``core.migrate`` carries the upgrade path.
SCHEMA_VERSION = 1

#: Contract between the daemon's control API and the GUI. The GUI refuses to talk to a
#: daemon advertising a different number rather than failing in confusing ways later.
API_VERSION = 1

#: Layout of ``netstate.json``, the record of which resolution rules we applied. Read by
#: ``--repair``, which must keep working against a file written by an older build.
NETSTATE_VERSION = 1
