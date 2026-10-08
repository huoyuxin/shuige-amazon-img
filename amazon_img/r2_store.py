from __future__ import annotations

import hashlib
import mimetypes
import re
from urllib.parse import quote, unquote, urlparse

import boto3
from botocore.config import Config as BotoConfig

from .config import Config


class R2Store:
    def __init__(self, config: Config):
        self.bucket_name = config.bucket
        self.base_url = config.public_base_url
        self.client = boto3.client(
            "s3", endpoint_url=f"https://{config.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=config.access_key_id, aws_secret_access_key=config.secret_access_key,
            region_name=config.region, config=BotoConfig(retries={"max_attempts": 4, "mode": "standard"}),
        )

    @staticmethod
    def object_key(source_url: str, content_type: str | None) -> str:
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        extension = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
        if extension == ".jpe":
            extension = ".jpg"
        return f"amazon-staging/{digest}{extension}"

    def upload(self, source_url: str, content: bytes, content_type: str | None) -> tuple[str, str]:
        key = self.object_key(source_url, content_type)
        extra = {"ContentType": content_type.split(";")[0].strip()} if content_type else {}
        self.client.put_object(Bucket=self.bucket_name, Key=key, Body=content, **extra)
        return key, self.public_url(key)

    def public_url(self, key: str) -> str:
        return f"{self.base_url}/{quote(key, safe='/')}"

    def key_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.base_url:
            raise ValueError(f"URL 不属于配置的 R2 域名：{url}")
        key = unquote(parsed.path.lstrip("/"))
        if not key or ".." in key.split("/"):
            raise ValueError(f"无效的 R2 对象 URL：{url}")
        return key

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket_name, Key=key)
