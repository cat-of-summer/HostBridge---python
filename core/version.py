r"""Version and schema constants.

``__version__`` is asserted against ``pyproject.toml`` by a unit test: the release tag
must match ``^v[0-9]+(\.[0-9]+)*$`` and a mislabelled binary is not something a user can
diagnose from the outside.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: On-disk layout of ``domains.json``. Bumped whenever ``core.model.Domain`` gains or
#: loses a persisted field; ``core.migrate`` carries the upgrade path.
SCHEMA_VERSION = 1

#: Contract between the daemon's control API and the GUI. The GUI refuses to talk to a
#: daemon advertising a different number rather than failing in confusing ways later.
API_VERSION = 1

#: Layout of ``netstate.json``, the record of which resolution rules we applied. Read by
#: ``--repair``, which must keep working against a file written by an older build.
NETSTATE_VERSION = 1
