import os
import re
import sys
import time
import json
import random
import queue
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import httpx

# 导入现有的经过充分验证的核心组件
import batch_download_huitun as bdh

PRIORITY_TASKS = [
    {
        "name": "点赞总榜-星图",
        "excel": Path(r"C:\Users\cuhksz\Desktop\灰豚数据\热门视频-点赞排序\点赞星图总榜.xlsx"),
        "dest": Path(r"D:\抖音视频下载\点赞总榜-星图"),
    },
    {
        "name": "分享榜-AI榜",
        "excel": Path(r"C:\Users\cuhksz\Desktop\灰豚数据\热门视频-分享排序\分享AI视频总榜.xlsx"),
        "dest": Path(r"D:\抖音视频下载\分享榜\2. AI榜"),
    },
    {
        "name": "分享榜-星图榜",
        "excel": Path(r"C:\Users\cuhksz\Desktop\灰豚数据\热门视频-分享排序\分享星图总榜.xlsx"),
        "dest": Path(r"D:\抖音视频下载\分享榜\3. 星图榜"),
    },
]

def download_single_table_multithreaded(
    excel_path: Path,
    dest_folder: Path,
    concurrency: int = 1,
    max_test: Optional[int] = None,
    *, anonymous_state=None, state_file=None, proxy="auto",
):
    """Compatibility entry point; share the stable engine and preserve the exact output folder.

    One browser processes the priority table. Retry failed/missing rows once;
    the shared engine rechecks successful files and never trusts stale flags.
    """
    args = [str(excel_path), "-o", str(dest_folder.parent),
            "--output-name", dest_folder.name, "--concurrency", "1", "--proxy", proxy]
    if max_test is not None:
        args += ["--test", str(max_test)]
    if anonymous_state is not None:
        args += ["--anonymous-state", str(anonymous_state)]
    elif state_file is not None:
        args += ["--state-file", str(state_file)]
    result = bdh.main(args)
    if result == 1 and max_test is None:
        print("首轮存在失败项，使用相同输出目录补跑一轮。", flush=True)
        result = bdh.main(args)
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="优先下载配置的表格，处理结束后接续默认目录批量任务")
    parser.add_argument("--test", type=int, default=None, help="仅测试前 N 个条目")
    parser.add_argument("--concurrency", type=bdh.positive_int, default=1, help="后续多表并发数（默认1）；优先表始终串行")
    parser.add_argument("--anonymous-state", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--proxy", default="auto")
    parser.add_argument("--no-resume", action="store_true", help="完成后不接续默认目录批量任务")
    args = parser.parse_args()

    failed = False
    # 步骤 1: 逐表使用公共下载入口。
    for t_idx, task_info in enumerate(PRIORITY_TASKS, 1):
        t_name = task_info["name"]
        t_excel = task_info["excel"]
        t_dest = task_info["dest"]

        print(f"\n" + "=" * 60)
        print(f"🎬 启动优先级任务 [{t_idx}/{len(PRIORITY_TASKS)}]: {t_name}")
        print(f"表格文件: {t_excel}")
        print(f"目标目录: {t_dest}")
        print(f"模式: 单表串行，完成后接续下一个")
        print("=" * 60 + "\n", flush=True)

        result = download_single_table_multithreaded(
            excel_path=t_excel,
            dest_folder=t_dest,
            concurrency=args.concurrency,
            max_test=args.test, anonymous_state=args.anonymous_state,
            state_file=args.state_file, proxy=args.proxy,
        )

        failed = failed or result != 0

    # 步骤 2: 按既有配置接续默认目录。失败通过退出码反馈。
    if not args.no_resume:
        print("\n" + "=" * 60)
        print("优先表处理结束。")
        print("开始后续默认目录的批量任务。")
        print("=" * 60 + "\n", flush=True)

        huitun_script = Path(__file__).parent / "batch_download_huitun.py"
        cmd = [sys.executable, str(huitun_script), "--concurrency", str(args.concurrency)]
        cmd += ["--proxy", args.proxy]
        if args.anonymous_state:
            cmd += ["--anonymous-state", str(args.anonymous_state)]
        elif args.state_file:
            cmd += ["--state-file", str(args.state_file)]
        if args.test is not None:
            cmd += ["--test", str(args.test)]
        failed = subprocess.run(cmd).returncode != 0 or failed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
