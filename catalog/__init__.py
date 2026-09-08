"""Product catalog subsystem for FBE.

SQLite is the source of truth. XLSX is only an import/export format.
"""

from .service import ProductCatalog

__all__ = ["ProductCatalog"]
