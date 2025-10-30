import cloudinary
import cloudinary.uploader
from fastapi import APIRouter, UploadFile, File, HTTPException
from app.config import settings

router = APIRouter(prefix="/articles", tags=["Uploads"])

# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
)

@router.post("/upload-image")
async def upload_article_image(file: UploadFile = File(...)):
    """Uploads an article image to Cloudinary and returns its URL."""

    allowed_extensions = {"jpg", "jpeg", "png", "gif", "webp"}
    extension = file.filename.split(".")[-1].lower()

    if extension not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Invalid file type")

    try:
        # Upload directly to Cloudinary
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="civcon/articles",
            resource_type="image",
        )

        # Cloudinary returns a secure URL
        return {"url": upload_result["secure_url"]}

    except Exception as e:
        print("Cloudinary upload error:", e)
        raise HTTPException(status_code=500, detail="Image upload failed")
