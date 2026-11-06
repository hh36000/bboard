from routing import public
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", operation_id="getHealth", description="Check HTTP liveness. This does not check database readiness.", responses={200: {"content": {"application/json": {"schema": {"type": "object", "required": ["status"], "properties": {"status": {"type": "string", "const": "healthy"}}}}}}})
@public
async def health_check():
    logger.info("Health check endpoint accessed")
    return JSONResponse({"status": "healthy"}, headers={"Cache-Control": "no-store"})
