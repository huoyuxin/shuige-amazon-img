from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _upload_one(url: str, cached_item: dict | None, store: R2Store, config: Config) -> dict:
    if cached_item:
        return dict(cached_item)
    content, content_type = _download(url, config)
    key, public_url = store.upload(url, content, content_type)
    return {"r2_url": public_url, "object_key": key, "status": "uploaded",
            "uploaded_at": datetime.now(timezone.utc).isoformat()}


def _log_progress(logger: logging.Logger, operation: str, task_id: str,
                  completed: int, total: int, status: str) -> None:
    remaining = max(0, total - completed)
    remaining_pct = remaining * 100 / total if total else 0.0
    completed_pct = completed * 100 / total if total else 100.0
    logger.info("%s进度 task_id=%s completed=%d/%d completed_pct=%.1f%% remaining=%d remaining_pct=%.1f%% status=%s",
                operation, task_id, completed, total, completed_pct, remaining, remaining_pct, status)


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
    reusable: dict[str, dict] = {}
    for previous_task in cache["tasks"].values():
        for previous_url, previous_item in previous_task.get("items", {}).items():
            if previous_item.get("status") == "uploaded":
                reusable.setdefault(previous_url, previous_item)

    total = len(urls)
    completed = successful = 0
    _log_progress(logger, "上传替换", task_id, 0, total, "started")
    executor = ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="image-upload")
    futures = {
        executor.submit(_upload_one, url, reusable.get(url), store, config): (url, cells)
        for url, cells in urls.items()
    }
    try:
        for future in as_completed(futures):
            url, cells = futures[future]
            status = "failed"
            try:
                item = future.result()
                cache["tasks"][task_id]["items"][url] = item
                save_cache(config.cache_file, cache)
                try:
                    for sheet, coordinate in cells:
                        editor.replace(sheet, coordinate, url, item["r2_url"])
                    successful += 1
                    status = "success"
                except Exception as error:
                    item["replacement_error"] = str(error)
                    cache["tasks"][task_id]["items"][url] = item
                    save_cache(config.cache_file, cache)
                    logger.error("工作簿替换失败 task_id=%s url=%s", task_id, _log_url(url),
                                 exc_info=(type(error), error, error.__traceback__))
                    status = "replacement_failed"
            except Exception as error:
                cache["tasks"][task_id]["items"][url] = {
                    "status": "failed", "cells": cells, "error": str(error)
                }
                save_cache(config.cache_file, cache)
                logger.error("处理失败 task_id=%s url=%s", task_id, _log_url(url),
                             exc_info=(type(error), error, error.__traceback__))
            completed += 1
            if completed % 25 == 0:
                editor.save()
            _log_progress(logger, "上传替换", task_id, completed, total, status)
        editor.save()
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        logger.warning("收到中断信号，正在保存工作簿与缓存 task_id=%s", task_id)
        editor.save()
        cache["tasks"][task_id]["status"] = "interrupted"
        cache["tasks"][task_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_cache(config.cache_file, cache)
        raise
    else:
        executor.shutdown(wait=True)
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
    by_key: dict[str, list[str]] = {}
    invalid_items: list[tuple[str, Exception]] = []
    for original_url, item in list(task.get("items", {}).items()):
        if item.get("status") != "uploaded":
            continue
        if original_url not in urls and item.get("r2_url") not in urls:
            logger.info("跳过清理：指定表格中未找到原链接 task_id=%s source_url=%s",
                        task_id, original_url)
            continue
        try:
            key = item.get("object_key") or store.key_from_url(item["r2_url"])
            by_key.setdefault(key, []).append(original_url)
        except Exception as error:
            invalid_items.append((original_url, error))

    total = len(by_key) + len(invalid_items)
    deleted = failed = completed = 0
    _log_progress(logger, "清理", task_id, 0, total, "started")
    for original_url, error in invalid_items:
        failed += 1
        completed += 1
        logger.error("清理目标无效 task_id=%s source_url=%s error=%s",
                     task_id, _log_url(original_url), error)
        _log_progress(logger, "清理", task_id, completed, total, "failed")

    executor = ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="r2-delete")
    futures = {executor.submit(store.delete, key): key for key in by_key}
    try:
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
                for original_url in by_key[key]:
                    task["items"].pop(original_url, None)
                save_cache(config.cache_file, cache)
                deleted += 1
                status = "success"
                logger.info("清理成功 task_id=%s object_key=%s mappings=%d",
                            task_id, key, len(by_key[key]))
            except Exception as error:
                failed += 1
                status = "failed"
                logger.error("清理失败 task_id=%s object_key=%s", task_id, key,
                             exc_info=(type(error), error, error.__traceback__))
            completed += 1
            _log_progress(logger, "清理", task_id, completed, total, status)
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        logger.warning("收到中断信号，保留未完成映射 task_id=%s", task_id)
        task["status"] = "cleanup_interrupted"
        task["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_cache(config.cache_file, cache)
        raise
    else:
        executor.shutdown(wait=True)
    task["status"] = "cleaned" if not task["items"] else "cleanup_partial"
    task["cleaned_at"] = datetime.now(timezone.utc).isoformat()
    save_cache(config.cache_file, cache)
    logger.info("清理结束 task_id=%s deleted=%d failed=%d", task_id, deleted, failed)
    return deleted, failed
