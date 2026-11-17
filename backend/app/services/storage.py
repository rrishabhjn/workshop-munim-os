from datetime import timedelta
from io import BytesIO
from functools import lru_cache

from google.cloud import storage
from PIL import Image, ImageOps

from app.config import get_settings

_LOCAL_MEM_BLOBS: dict[str, bytes] = {}
_MAX_LOCAL_MEM_BLOBS = 64


@lru_cache
def _get_gcs_client() -> storage.Client:
    settings = get_settings()
    return storage.Client(project=settings.gcp_project_id or None)


def _remember_local_blob(object_path: str, data: bytes) -> None:
    if len(_LOCAL_MEM_BLOBS) >= _MAX_LOCAL_MEM_BLOBS:
        _LOCAL_MEM_BLOBS.pop(next(iter(_LOCAL_MEM_BLOBS)))
    _LOCAL_MEM_BLOBS[object_path] = data


def compress_image_to_webp(
    raw_bytes: bytes, max_dim: int = 1600, quality: int = 80
) -> bytes:
    """Resize an image so its longest edge is <= max_dim and encode as WebP."""
    with Image.open(BytesIO(raw_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = BytesIO()
        img.save(out, format="WEBP", quality=quality, method=6)
        return out.getvalue()


def upload_bytes(
    object_path: str, data: bytes, content_type: str = "application/octet-stream"
) -> str:
    """Upload bytes to the private GCS bucket and return the object path."""
    _remember_local_blob(object_path, data)
    settings = get_settings()
    client = _get_gcs_client()
    bucket = client.bucket(settings.gcs_storage_bucket)
    blob = bucket.blob(object_path)
    blob.upload_from_string(data, content_type=content_type)
    return object_path


def upload_image_as_webp(object_path: str, raw_bytes: bytes) -> str:
    """Compress an image to WebP (max 1600px) and upload to private GCS."""
    webp_bytes = compress_image_to_webp(raw_bytes)
    return upload_bytes(object_path, webp_bytes, content_type="image/webp")


def download_bytes(object_path: str) -> bytes:
    """Download object bytes from private GCS (with local dev/test memory fallback)."""
    cached = _LOCAL_MEM_BLOBS.get(object_path)
    if cached is not None:
        return cached
    settings = get_settings()
    if settings.gcs_storage_bucket:
        try:
            client = _get_gcs_client()
            bucket = client.bucket(settings.gcs_storage_bucket)
            data = bucket.blob(object_path).download_as_bytes()
            _remember_local_blob(object_path, data)
            return data
        except Exception:
            if object_path in _LOCAL_MEM_BLOBS:
                return _LOCAL_MEM_BLOBS[object_path]
            raise
    raise FileNotFoundError(f"Object '{object_path}' not found in local cache or GCS.")


def generate_signed_url(
    object_path: str, expiration_minutes: int = 30
) -> str:
    """Generate a short-lived V4 signed GET URL for a private GCS object."""
    settings = get_settings()
    client = _get_gcs_client()
    bucket = client.bucket(settings.gcs_storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
    )
