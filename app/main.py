import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, engine
from app.models import *  # noqa: F401,F403 — ensure all models are registered
from app.api import health, benchmark, employers, data_pipeline, clinical, price_discovery, providers, cost_prediction, claims, pricing, care
from app.api import shadow, dashboard, broker_channel, benefits_admin

logging.basicConfig(level=logging.INFO)

# Create tables if they don't exist (dev convenience; use Alembic in production)
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch background data pipeline scheduler
    from app.workers.scheduler import start_scheduler, stop_scheduler
    start_scheduler()
    yield
    # Shutdown: stop scheduler
    stop_scheduler()


app = FastAPI(
    title="First Principles API",
    description="Benefits platform — 10x cheaper total cost of benefits ownership",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HIPAA audit logging — logs every API request (no PHI in logs)
from app.middleware.audit import AuditLoggingMiddleware
app.add_middleware(AuditLoggingMiddleware)

app.include_router(health.router)
app.include_router(benchmark.router, prefix="/api/v1")
app.include_router(employers.router, prefix="/api/v1")
app.include_router(data_pipeline.router, prefix="/api/v1")
app.include_router(clinical.router, prefix="/api/v1")
app.include_router(price_discovery.router, prefix="/api/v1")
app.include_router(providers.router, prefix="/api/v1")
app.include_router(cost_prediction.router, prefix="/api/v1")
app.include_router(claims.router, prefix="/api/v1")
app.include_router(pricing.router, prefix="/api/v1")
app.include_router(care.router, prefix="/api/v1")
app.include_router(shadow.router, prefix="/api/v1")
app.include_router(dashboard.router, prefix="/api/v1")
app.include_router(broker_channel.router, prefix="/api/v1")
app.include_router(benefits_admin.router, prefix="/api/v1")
