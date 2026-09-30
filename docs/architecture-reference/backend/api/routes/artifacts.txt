"""Protected access to execution artifacts."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse


router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_path:path}", summary="Download an artifact file")
def download_artifact(
    artifact_path: str,
    request: Request,
) -> FileResponse:
    artifacts_dir = Path(request.app.state.artifacts_dir).resolve()
    candidate = (artifacts_dir / artifact_path).resolve()

    try:
        candidate.relative_to(artifacts_dir)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found.") from exc

    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found.")

    return FileResponse(candidate)
