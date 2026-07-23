"""
EDP Billing API package.

Assembles the three sub-routers under the /edp prefix.
Import `router` from here to mount in the main FastAPI app.
"""

from fastapi import APIRouter

from .audit import router as _audit_router
from .control import router as _control_router
from .status import router as _status_router
from .workflow import router as _workflow_router

router = APIRouter(prefix="/edp", tags=["EDP Billing"])

router.include_router(_workflow_router)
router.include_router(_status_router)
router.include_router(_control_router)
router.include_router(_audit_router)

__all__ = ["router"]
