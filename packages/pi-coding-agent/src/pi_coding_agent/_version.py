"""Version metadata.

Kept in its own module so the CLI can report a version without importing the
package's lazy export surface.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
