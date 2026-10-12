"""The schema library HSEM builds its flow and service schemas with.

Home Assistant 2026.10 replaced voluptuous with probatio, a drop-in with the
same API, and aliases ``voluptuous`` to it at start-up for integrations that
still import it (``probatio.compat.install_as_voluptuous`` in
``homeassistant/__init__.py``).  Home Assistant's own signatures name
``probatio.Schema``.

HSEM supports Home Assistant versions on both sides of that change, so:

- **at runtime** ``vol`` is ``voluptuous``, which is the real library before
  2026.10 and probatio's alias from then on.  Probatio itself is never
  imported here: older Home Assistant versions do not ship it;
- **for type checkers** ``vol`` is ``probatio``, the library the pinned Home
  Assistant's signatures are written against.  A checker cannot see the
  runtime alias, and would otherwise report every schema handed to Home
  Assistant as the wrong type (issue #1289).

Import ``vol`` from here.  No other module imports ``voluptuous`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import probatio as vol
else:
    import voluptuous as vol

__all__ = ["vol"]
