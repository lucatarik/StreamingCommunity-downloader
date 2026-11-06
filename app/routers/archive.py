import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.core import animeunity, archive

logger = logging.getLogger(__name__)

# The archive is browsing for something to request or download, so it is open to
# whoever may search: either privilege grants it.
router = APIRouter(
    prefix="/api/archive",
    tags=["archive"],
    dependencies=[Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))],
)

_SOURCE = Query(default="streamingcommunity", pattern="^(streamingcommunity|animeunity)$")


def _host(source: str) -> str:
    """The source's host. Never from the caller: it decides where we send requests."""
    if source == "animeunity":
        # Has a default host and needs nothing configured.
        return animeunity.host()
    host = configured_domain()
    if not host:
        raise HTTPException(status_code=409, detail="Nessun dominio configurato")
    return host


@router.get("/filters")
async def filters(source: str = _SOURCE):
    """The filters this source's archive offers, with their options and defaults."""
    host = _host(source)
    try:
        return await asyncio.to_thread(archive.definition, source, host)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Archive filters error")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("")
async def browse(request: Request, source: str = _SOURCE,
                 page: int = Query(default=1, ge=1, le=archive.AU_MAX_PAGES)):
    """One page of the archive. The filters are the query string's other keys.

    Their names and allowed values come from ``/filters``; anything outside
    those is refused (422) rather than forwarded to the source as typed.
    """
    host = _host(source)
    raw: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        # The two that are the route's own, not filters.
        if key not in ("source", "page"):
            raw.setdefault(key, []).append(value)

    try:
        return await asyncio.to_thread(archive.browse, source, host, raw, page)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Archive error")
        raise HTTPException(status_code=502, detail=str(e))
