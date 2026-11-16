"""PDF resume upload for the signed-in user, stored on local disk."""
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse

from workwise.api.dependencies import DbSession
from workwise.api.routes.applications import find_application
from workwise.api.schemas import ResumeRead
from workwise.core import resume_store
from workwise.core.security import CurrentUser, auth_settings
from workwise.db.models import UserResume

router = APIRouter(tags=["resume"])
PRIVATE = {"Cache-Control": "private, no-store"}
CHUNK = 64 * 1024


def read_pdf(upload: UploadFile):
    """Yield chunks of a valid PDF no larger than the limit; raise HTTPException otherwise."""
    limit = auth_settings.resume_max_bytes
    total, digest, chunks = 0, hashlib.sha256(), []
    head = b""
    tail = b""
    while chunk := upload.file.read(CHUNK):
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"Resume must be at most {limit} bytes")
        if len(head) < 5:
            head = (head + chunk)[:5]
        tail = (tail + chunk)[-1024:]
        digest.update(chunk)
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(422, "Resume file is empty")
    if head != b"%PDF-" or b"%%EOF" not in tail:
        raise HTTPException(415, "Only PDF files are accepted")
    return chunks, total, digest.hexdigest()


@router.put("/resume", response_model=ResumeRead)
def upload_resume(file: UploadFile, request: Request, response: Response, db: DbSession, user: CurrentUser) -> UserResume:
    """Create or replace your resume. Accepts a PDF up to RESUME_MAX_BYTES."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > auth_settings.resume_max_bytes + 4096:
        raise HTTPException(413, "Resume is too large")
    response.headers.update(PRIVATE)
    chunks, size, sha256 = read_pdf(file)
    key, _ = resume_store.save(chunks)
    existing = db.get(UserResume, user.id)
    previous = existing.storage_key if existing else None
    try:
        if existing is None:
            existing = UserResume(user_id=user.id)
            db.add(existing)
        existing.storage_key, existing.size_bytes, existing.sha256 = key, size, sha256
        existing.uploaded_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        resume_store.remove(key)
        raise
    if previous:
        resume_store.remove(previous)
    db.refresh(existing)
    return existing


def send_resume(resume: UserResume | None) -> FileResponse:
    if resume is None:
        raise HTTPException(404, "Resume not found", headers=PRIVATE)
    path = resume_store.path_for(resume.storage_key)
    if not path.exists():
        raise HTTPException(404, "Resume not found", headers=PRIVATE)
    return FileResponse(path, media_type="application/pdf", filename="resume.pdf", headers=PRIVATE)


@router.get("/resume")
def download_resume(db: DbSession, user: CurrentUser) -> FileResponse:
    return send_resume(db.get(UserResume, user.id))


@router.delete("/resume", status_code=204)
def delete_resume(db: DbSession, user: CurrentUser) -> Response:
    existing = db.get(UserResume, user.id)
    if existing is None:
        raise HTTPException(404, "Resume not found")
    key = existing.storage_key
    db.delete(existing)
    db.commit()
    resume_store.remove(key)
    return Response(status_code=204, headers=PRIVATE)


@router.get("/applications/{application_id}/resume")
def download_applicant_resume(application_id: int, db: DbSession, user: CurrentUser) -> FileResponse:
    """The applicant, the job's company owner and admins may download the applicant's current resume."""
    application = find_application(db, application_id)
    if user.id != application.applicant_id and user.role != "admin" and application.job.employer.owner_id != user.id:
        raise HTTPException(404, "Application not found")
    return send_resume(db.get(UserResume, application.applicant_id))

