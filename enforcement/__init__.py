"""Data-access enforcement: one access policy per table, applied to every query.

The package has four small pieces:

* :mod:`enforcement.db`        - seeds the demo ``employees`` table in SQLite.
* :mod:`enforcement.identity`  - maps a login to a user and their groups.
* :mod:`enforcement.policies`  - loads policies and rewrites SQL to honour them.
* :mod:`enforcement.resource`  - the enforced resource: identity + policy + audit.
* :mod:`enforcement.gateway`   - a tiny HTTP front door so apps never see the DB.
"""

from .resource import EnforcedResource, AccessDenied  # noqa: F401
