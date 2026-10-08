from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import requests

from .cache import load_cache, save_cache
from .config import Config
from .r2_store import R2Store
from .workbook import WorkbookEditor, create_workbook_copy, find_image_urls, make_copy_path


def _download(url: str, config: Config) -> tuple[bytes, str | None]:
    response = requests.get(url, timeout=config.request_timeout_seconds,
                            headers={"User-Agent": config.user_agent}, stream=True)
    response.raise_for_status()
    content = response.content
    if not content:
        raise ValueError("下载到空图片")
    content_type = response.headers.get("Content-Type")
    if content_type and not content_type.lower().startswith("image/"):
        raise ValueError(f"链接返回的不是图片内容：{content_type}")
    return content, content_type


def _log_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def upload_and_replace(source: Path, config: Config, logger: logging.Logger) -> tuple[str, Path]:
    source = source.resolve()
    if source.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 .xlsx 和 .xlsm 表格")
    task_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    copy_path = make_copy_path(source, task_id)
    create_workbook_copy(source, copy_path)
    urls = find_image_urls(copy_path, config.image_url_regex)
    cache = load_cache(config.cache_file)
    cache["tasks"][task_id] = {
        "source_file": str(source), "copy_file": str(copy_path),
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "running", "items": {},
    }
    save_cache(config.cache_file, cache)
    logger.info("任务开始 task_id=%s source_file=%s copy_file=%s unique_urls=%d",
                task_id, source, copy_path, len(urls))
    store = R2Store(config)
    editor = WorkbookEditor(copy_path)
    successful = 0
    try:
        for url, cells in urls.items():
            item = cache["tasks"][task_id]["items"].get(url)
            if item and item.get("status") != "uploaded":
                item = None
            if not item:
                # Reuse a prior successful upload for the same source URL.
                for old_task in cache["tasks"].values():
                    old = old_task.get("items", {}).get(url)
                    if old and old.get("status") == "uploaded":
                        item = dict(old)
                        break
            try:
                if not item:
                    content, content_type = _download(url, config)
                    key, public_url = store.upload(url, content, content_type)
                    item = {"r2_url": public_url, "object_key": key, "status": "uploaded",
                            "uploaded_at": datetime.now(timezone.utc).isoformat()}
                cache["tasks"][task_id]["items"][url] = item
                save_cache(config.cache_file, cache)
                for sheet, coordinate in cells:
                    editor.replace(sheet, coordinate, url, item["r2_url"])
                successful += 1
                if successful % 25 == 0:
                    editor.save()
                logger.info("上传替换成功 task_id=%s url=%s cells=%d r2_url=%s",
                            task_id, _log_url(url), len(cells), item["r2_url"])
            except KeyboardInterrupt:
                logger.warning("收到中断信号，正在保存工作簿与缓存 task_id=%s", task_id)
                editor.save()
                cache["tasks"][task_id]["status"] = "interrupted"
                cache["tasks"][task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_cache(config.cache_file, cache)
                raise
            except Exception:
                cache["tasks"][task_id]["items"][url] = {"status": "failed", "cells": cells}
                save_cache(config.cache_file, cache)
                logger.exception("处理失败 task_id=%s url=%s", task_id, _log_url(url))
        editor.save()
    finally:
        editor.close()
    task = cache["tasks"][task_id]
    task["status"] = "complete" if successful == len(urls) else "partial"
    save_cache(config.cache_file, cache)
    logger.info("任务结束 task_id=%s status=%s success=%d total=%d output=%s",
                task_id, task["status"], successful, len(urls), copy_path)
    return task_id, copy_path


def cleanup_task(task_id: str, source: Path, config: Config, logger: logging.Logger) -> tuple[int, int]:
    source = source.resolve()
    if source.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 .xlsx 和 .xlsm 表格")
    cache = load_cache(config.cache_file)
    task = cache["tasks"].get(task_id)
    if not task:
        raise ValueError(f"缓存中没有任务 ID：{task_id}")
    source_file = Path(task["source_file"]).resolve()
    copy_file = Path(task["copy_file"]).resolve()
    if source not in {source_file, copy_file}:
        raise ValueError("指定表格路径与该任务记录的 source_file/copy_file 均不匹配")
    # Scan the chosen workbook for source links or their replaced public R2 links.
    pattern = config.image_url_regex if source == source_file else re.compile(re.escape(config.public_base_url))
    urls = find_image_urls(source, pattern)
    logger.info("清理开始 task_id=%s workbook=%s matched_urls=%d", task_id, source, len(urls))
    store = R2Store(config)
    deleted = failed = 0
    for original_url, item in list(task.get("items", {}).items()):
        if item.get("status") != "uploaded":
            continue
        if original_url not in urls and item.get("r2_url") not in urls:
            logger.info("跳过清理：指定表格中未找到原链接 task_id=%s source_url=%s",
                        task_id, original_url)
            continue
        try:
            key = item.get("object_key") or store.key_from_url(item["r2_url"])
            store.delete(key)
            del task["items"][original_url]
            save_cache(config.cache_file, cache)
            deleted += 1
            logger.info("清理成功 task_id=%s object_key=%s source_url=%s",
                        task_id, key, _log_url(original_url))
        except KeyboardInterrupt:
            logger.warning("收到中断信号，保留未完成映射 task_id=%s", task_id)
            task["status"] = "cleanup_interrupted"
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_cache(config.cache_file, cache)
            raise
        except Exception:
            failed += 1
            logger.exception("清理失败 task_id=%s source_url=%s", task_id, _log_url(original_url))
    task["status"] = "cleaned" if not task["items"] else "cleanup_partial"
    task["cleaned_at"] = datetime.now(timezone.utc).isoformat()
    save_cache(config.cache_file, cache)
    logger.info("清理结束 task_id=%s deleted=%d failed=%d", task_id, deleted, failed)
    return deleted, failed
