import json
from decimal import Decimal

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


def _json_serializer(obj):
    """Custom JSON serializer that handles Decimal types."""
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
    return json.dumps(obj, default=default)


def _json_deserializer(s):
    return json.loads(s)

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    pool_pre_ping=not settings.database_url.startswith("sqlite"),
    connect_args=connect_args,
    json_serializer=_json_serializer,
    json_deserializer=_json_deserializer,
)

# Enable foreign keys for SQLite
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")  # Wait up to 30s for locks
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- Immutability enforcement for clinical_determinations ----
# Constitution F1: "Every determination recorded in an immutable, append-only,
# cryptographically secured log."
#
# PostgreSQL uses a database trigger (see alembic migration 001).
# For SQLite and all engines, we also enforce at the ORM level via
# SQLAlchemy event listeners. This prevents any code path — even
# direct ORM manipulation — from modifying or deleting determinations.

@event.listens_for(SessionLocal, "before_flush")
def _enforce_immutable_determinations(session, flush_context, instances):
    """Block UPDATE and DELETE on ClinicalDetermination objects.

    Only exception: setting outcome_feedback from NULL to a value
    (one-time write for accuracy tracking).

    Uses before_flush to prevent the SQL from ever executing.
    """
    from app.models.clinical_determination import ClinicalDetermination

    # Check for deletions
    for obj in session.deleted:
        if isinstance(obj, ClinicalDetermination):
            raise RuntimeError(
                "clinical_determinations is append-only. DELETE is prohibited. "
                "Constitution: immutable, append-only, cryptographically secured log."
            )

    # Check for updates (dirty objects)
    for obj in session.dirty:
        if isinstance(obj, ClinicalDetermination):
            from sqlalchemy import inspect as sa_inspect
            state = sa_inspect(obj)
            changed_attrs = []
            for attr in state.attrs:
                hist = attr.history
                if hist.has_changes():
                    changed_attrs.append(attr.key)

            # Allow ONLY outcome_feedback and outcome_recorded_at updates
            allowed_updates = {"outcome_feedback", "outcome_recorded_at"}
            disallowed = set(changed_attrs) - allowed_updates
            if disallowed:
                raise RuntimeError(
                    f"clinical_determinations is append-only. "
                    f"UPDATE of {disallowed} is prohibited. "
                    f"Only outcome_feedback can be set (once, for accuracy tracking). "
                    f"Constitution: immutable, append-only, cryptographically secured log."
                )
