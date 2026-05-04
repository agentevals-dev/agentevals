"""HTTP router for the async run pipeline.

Mounted only when ``AGENTEVALS_STORAGE_BACKEND=postgres``. Submission is
idempotent on ``run_id``: re-posting the same id with an identical spec
returns the persisted row; re-posting with a different spec returns
``409 Conflict``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from ..run.service import RunService, RunSubmitConflict
from ..storage.models import Result, Run, RunSpec, RunStatus
from .models import CamelModel, StandardResponse

logger = logging.getLogger(__name__)

runs_router = APIRouter(tags=["runs"])


class RunRequest(CamelModel):
    """POST body for ``/api/runs``."""

    run_id: UUID | None = None
    spec: RunSpec

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class RunSummary(CamelModel):
    run_id: UUID
    status: RunStatus
    created_at: datetime


def _service(request: Request) -> RunService:
    service = getattr(request.app.state, "run_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run service is not configured (set AGENTEVALS_STORAGE_BACKEND=postgres)",
        )
    return service


@runs_router.post(
    "/runs",
    response_model=StandardResponse[Run],
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_run(payload: RunRequest, request: Request):
    service = _service(request)
    try:
        run = await service.submit(run_id=payload.run_id, spec=payload.spec)
    except RunSubmitConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "run_id already exists with a different spec",
                "persisted": exc.persisted.model_dump(mode="json", by_alias=True),
            },
        ) from exc
    return StandardResponse(data=run)


@runs_router.get("/runs/{run_id}", response_model=StandardResponse[Run])
async def get_run(run_id: UUID, request: Request):
    service = _service(request)
    run = await service.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
    return StandardResponse(data=run)


@runs_router.get("/runs", response_model=StandardResponse[list[Run]])
async def list_runs(
    request: Request,
    status_filter: list[RunStatus] | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    before: datetime | None = Query(default=None),
):
    service = _service(request)
    runs = await service.list(status=status_filter, limit=limit, before=before)
    return StandardResponse(data=runs)


@runs_router.get("/runs/{run_id}/results", response_model=StandardResponse[list[Result]])
async def list_run_results(run_id: UUID, request: Request):
    service = _service(request)
    run = await service.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
    results = await service.list_results(run_id)
    return StandardResponse(data=results)


@runs_router.post("/runs/{run_id}/cancel", response_model=StandardResponse[Run])
async def cancel_run(run_id: UUID, request: Request):
    service = _service(request)
    cancelled = await service.cancel(run_id)
    run = await service.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"run {run_id} not found")
    if not cancelled and run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
        # Already terminal; surface that to the caller without an error.
        return StandardResponse(data=run)
    return StandardResponse(data=run)
