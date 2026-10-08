from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import load_config
from .logging_setup import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon 批量刊登图片暂存工具")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    upload = sub.add_parser("upload", help="上传图片并生成替换后的表格副本")
    upload.add_argument("workbook", type=Path)
    cleanup = sub.add_parser("cleanup", help="按任务 ID 删除本次暂存的图片")
    cleanup.add_argument("workbook", type=Path)
    cleanup.add_argument("task_id")
    args = parser.parse_args()
    logger = configure_logging(Path("logs/amazon_img.log"))
    try:
        config = load_config(args.config)
        if config.log_file != Path("logs/amazon_img.log"):
            logger = configure_logging(config.log_file)
        if args.command == "upload":
            from .service import upload_and_replace

            task_id, copy_path = upload_and_replace(args.workbook, config, logger)
            print(f"任务 ID：{task_id}\n表格副本：{copy_path}")
        else:
            from .service import cleanup_task

            deleted, failed = cleanup_task(args.task_id, args.workbook, config, logger)
            print(f"已删除：{deleted}，失败：{failed}")
        return 0
    except KeyboardInterrupt:
        logging.getLogger("amazon_img").warning("程序中断；已完成记录已保存，可按日志和缓存恢复。")
        return 130
    except Exception as exc:
        logging.getLogger("amazon_img").exception("运行失败：%s", exc)
        print(f"错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
