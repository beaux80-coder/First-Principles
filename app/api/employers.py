from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/employers", tags=["employers"])


@router.get("/me")
def get_current_employer(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get the current authenticated employer's dashboard. Placeholder for Phase 0."""
    return {
        "user": user.get("sub"),
        "message": "Dashboard coming in Phase 1+",
    }
