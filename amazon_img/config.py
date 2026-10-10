from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str
    region: str
    image_url_regex: re.Pattern[str]
    cache_file: Path
    log_file: Path
    request_timeout_seconds: int
    max_workers: int
    user_agent: str


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    r2 = raw["r2"]
    required = ("account_id", "access_key_id", "secret_access_key", "bucket", "public_base_url")
    missing = [name for name in required if not str(r2.get(name, "")).strip() or str(r2[name]).startswith("填写 ")]
    if missing:
        raise ValueError("请先在配置文件中填写 R2 字段：" + ", ".join(missing))
    base = r2["public_base_url"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("r2.public_base_url 必须使用 HTTPS")
    pattern = re.compile(raw["image_url_regex"])
    return Config(
        account_id=r2["account_id"], access_key_id=r2["access_key_id"],
        secret_access_key=r2["secret_access_key"], bucket=r2["bucket"],
        public_base_url=base, region=r2.get("region", "auto"),
        image_url_regex=pattern,
        cache_file=Path(raw.get("cache_file", "data/image_cache.json")),
        log_file=Path(raw.get("log_file", "logs/amazon_img.log")),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        max_workers=max(1, int(raw.get("max_workers", 50))),
        user_agent=raw.get("user_agent", "AmazonImageUploader/1.0"),
    )
