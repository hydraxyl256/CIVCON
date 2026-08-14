import asyncio

import cloudinary
import cloudinary.uploader
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi_limiter.depends import RateLimiter

from app.config import settings
from app.dependencies.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/articles", tags=["Uploads"])

# Configure Cloudinary
# SECURITY (F-003): pin `secure=True` so Cloudinary returns `https://`
# URLs only. Without this, the SDK can return `http://` for legacy
# accounts, which the SPA would then load over plaintext.
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
    secure=True,
)

# SECURITY (F-003): the previous version of this endpoint accepted
# anonymous multipart uploads. That let any internet client fill the
# CIV-CON Cloudinary bucket, drive up billing, and inject arbitrary
# image content under the legitimate Cloudinary domain. We now:
#   1. Require an authenticated user (`get_current_user`).
#   2. Apply a per-user rate limit (20/min) — above any legitimate
#      author workflow, well below abuse.
#   3. Validate the Content-Type header against an allowlist, not
#      just the filename extension (filenames can lie).
#   4. Cap body size (the global 10 MiB applies; for images we
#      additionally enforce a 5 MiB cap inside the handler).
_ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
_ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB

# SECURITY (F-003): gate by user role. Only journalists and admins can
# upload article imagery. Citizens who paste images into the rich-text
# editor should go through the post-media upload (which has its own
# gated path).
_ARTICLE_UPLOAD_ROLES = {"journalist", "admin", "moderator"}


def _require_article_uploader(current_user: User) -> None:
    role = (getattr(current_user.role, "value", None) or "").lower()
    if role not in _ARTICLE_UPLOAD_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Article image upload requires a journalist, moderator, or admin account.",
        )


@router.post(
    "/upload-image",
    dependencies=[Depends(RateLimiter(times=20, minutes=1))],
)
async def upload_article_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Uploads an article image to Cloudinary and returns its URL."""

    _require_article_uploader(current_user)

    # 1. Validate Content-Type header (the upload's claimed MIME).
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Content-Type {content_type!r}. Allowed: {sorted(_ALLOWED_CONTENT_TYPES)}",
        )

    # 2. Validate extension as a second line of defence (some browsers
    # send application/octet-stream; reject those).
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension {extension!r}. Allowed: {sorted(_ALLOWED_EXTENSIONS)}",
        )

    # 3. Enforce a per-upload size cap before passing the file to
    # Cloudinary, so a 10 MiB upload doesn't bill us for a 10 MiB
    # transfer before being rejected.
    file.file.seek(0, 2)  # seek to end
    size = file.file.tell()
    file.file.seek(0)  # reset
    if size > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds maximum size of {_MAX_IMAGE_BYTES // (1024 * 1024)} MiB",
        )
    if size == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        # Perf: Cloudinary's `upload` is a blocking SDK call that can
        # take seconds. Run it in a thread so the event loop is not
        # pinned while the request is in flight — same response,
        # higher concurrency under load.
        upload_result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            file.file,
            folder="civcon/articles",
            resource_type="image",
        )

        # Cloudinary returns a secure URL
        return {"url": upload_result["secure_url"]}

    except Exception as e:
        print("Cloudinary upload error:", e)
        raise HTTPException(status_code=500, detail="Image upload failed") from e
