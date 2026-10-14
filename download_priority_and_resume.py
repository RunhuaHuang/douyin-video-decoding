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

TARGET_EXCEL = Path(r"C:\Users\cuhksz\Desktop\灰豚数据\热门视频-点赞排序\点赞星图总榜.xlsx")
TARGET_DEST = Path(r"D:\抖音视频下载\点赞总榜-星图")

def save_manifest_atomic(manifest_path: Path, manifest: dict):
    tmp = manifest_path.with_name(f"manifest_{int(time.time()*1000)}.tmp")
    try:
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(manifest_path)
    except Exception as e:
        try:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        if tmp.exists():
            tmp.unlink(missing_ok=True)

def worker_thread(
    worker_id: int,
    task_queue: queue.Queue,
    total_tasks: int,
    folder_dest: Path,
    manifest: dict,
    manifest_lock: threading.Lock,
    manifest_path: Path,
    counters: dict,
    counters_lock: threading.Lock,
):
    worker = bdh.TableWorker(worker_id, folder_dest.parent)
    tag = f"工作线程 {worker_id} | 点赞星图"
    consecutive_fails = 0

    try:
        while True:
            try:
                task = task_queue.get_nowait()
            except queue.Empty:
                break

            idx, rank, video_id, raw_url, title = task

            # 1. 检查物理文件是否已存在
            vid_target = folder_dest / f"视频-{video_id}.mp4"
            note_target = folder_dest / f"图文-{video_id}.mp4"

            if (vid_target.exists() and vid_target.stat().st_size > 10240) or (note_target.exists() and note_target.stat().st_size > 10240):
                with counters_lock:
                    counters["skip"] += 1
                with manifest_lock:
                    if video_id not in manifest or manifest[video_id].get("status") != "success":
                        manifest[video_id] = {
                            "status": "success",
                            "file": vid_target.name if vid_target.exists() else note_target.name,
                            "title": title
                        }
                task_queue.task_done()
                continue

            # 2. 检查 manifest 历史状态
            with manifest_lock:
                entry = manifest.get(video_id)
            if entry:
                if entry.get("status") == "success":
                    with counters_lock:
                        counters["skip"] += 1
                    task_queue.task_done()
                    continue
                if entry.get("status") == "deleted":
                    with counters_lock:
                        counters["deleted"] += 1
                    task_queue.task_done()
                    continue

            # 3. 开始解析元数据
            clean_title = re.sub(r'[\r\n\t]+', ' ', title).strip()[:25]
            print(f"[{tag}] [{idx+1}/{total_tasks}] 解析第{rank}名 (ID: {video_id}) [{clean_title}]...", flush=True)

            det, err = worker.resolve_video_info(video_id)

            if err:
                if "已删除" in err or "不可见" in err:
                    with counters_lock:
                        counters["deleted"] += 1
                    with manifest_lock:
                        manifest[video_id] = {"status": "deleted", "reason": err, "title": title}
                        if counters["deleted"] % 5 == 0:
                            save_manifest_atomic(manifest_path, manifest)
                    print(f"[{tag}]   -> ⚠️ {err}", flush=True)
                elif "触发验证码风控" in err:
                    manual_ok = bdh.WafCoordinator.trigger_cooldown(tag, duration=180.0)
                    if worker_id > 1:
                        time.sleep(random.uniform(3.0, 5.0))
                    worker.start_browser()
                    det, err = worker.resolve_video_info(video_id)
                    if err and "触发验证码风控" in err and not manual_ok:
                        manual_ok = bdh.WafCoordinator.trigger_cooldown(tag, duration=300.0)
                        if worker_id > 1:
                            time.sleep(random.uniform(3.0, 5.0))
                        worker.start_browser()
                        det, err = worker.resolve_video_info(video_id)

                    if err:
                        with counters_lock:
                            counters["fail"] += 1
                        consecutive_fails += 1
                        with manifest_lock:
                            manifest[video_id] = {"status": "failed", "reason": err, "title": title}
                            save_manifest_atomic(manifest_path, manifest)
                        print(f"[{tag}]   -> ❌ 静默后仍受限，标记为失败: {err}", flush=True)
                        task_queue.task_done()
                        continue
                else:
                    with counters_lock:
                        counters["fail"] += 1
                    consecutive_fails += 1
                    with manifest_lock:
                        manifest[video_id] = {"status": "failed", "reason": err, "title": title}
                        if counters["fail"] % 5 == 0:
                            save_manifest_atomic(manifest_path, manifest)
                    print(f"[{tag}]   -> ❌ 解析失败: {err}", flush=True)

                    if consecutive_fails >= 5:
                        print(f"[{tag}] ⚠️ 检测到连续 {consecutive_fails} 次解析失败，冷却 20 秒并重启浏览器...", flush=True)
                        worker.close_browser()
                        time.sleep(20.0)
                        worker.start_browser()
                        consecutive_fails = 0
                    else:
                        time.sleep(random.uniform(1.0, 2.0))

                    task_queue.task_done()
                    continue

            consecutive_fails = 0

            # 4. 判断类型：图文 note 还是普通视频
            aweme_type = det.get("aweme_type")
            images = det.get("images")
            is_note = (aweme_type == 68) or (bool(images) and isinstance(images, list) and len(images) > 0)

            if is_note:
                img_urls = []
                if isinstance(images, list):
                    for img_item in images:
                        if isinstance(img_item, dict) and img_item.get("url_list"):
                            img_urls.append(img_item["url_list"][-1])
                bgm_urls = det.get("music", {}).get("play_url", {}).get("url_list", [])

                if not img_urls:
                    with counters_lock:
                        counters["fail"] += 1
                    with manifest_lock:
                        manifest[video_id] = {"status": "failed", "reason": "未找到图文图片链接", "title": title}
                    print(f"[{tag}]   -> ❌ 图文未找到有效图片链接", flush=True)
                    task_queue.task_done()
                    continue

                ok = worker.synthesize_note_mp4(img_urls, bgm_urls, note_target)
                if ok:
                    size_mb = note_target.stat().st_size / (1024 * 1024)
                    with counters_lock:
                        counters["success"] += 1
                    with manifest_lock:
                        manifest[video_id] = {
                            "status": "success",
                            "file": note_target.name,
                            "type": "note",
                            "size_mb": round(size_mb, 2),
                            "title": title
                        }
                        save_manifest_atomic(manifest_path, manifest)
                    print(f"[{tag}]   -> 🖼️✅ 图文合成成功: {note_target.name} ({len(img_urls)}图, {size_mb:.1f} MB)", flush=True)
                else:
                    with counters_lock:
                        counters["fail"] += 1
                    with manifest_lock:
                        manifest[video_id] = {"status": "failed", "reason": "图文合成失败", "title": title}
                        save_manifest_atomic(manifest_path, manifest)
                    print(f"[{tag}]   -> ❌ 图文合成失败", flush=True)

            else:
                play_addr = det.get("video", {}).get("play_addr", {})
                cdn_urls = play_addr.get("url_list", [])
                if not cdn_urls:
                    with counters_lock:
                        counters["fail"] += 1
                    with manifest_lock:
                        manifest[video_id] = {"status": "failed", "reason": "未找到可用视频流", "title": title}
                    print(f"[{tag}]   -> ❌ 未找到可用视频流", flush=True)
                    task_queue.task_done()
                    continue

                ok = worker.download_video_stream(cdn_urls, vid_target)
                if ok:
                    size_mb = vid_target.stat().st_size / (1024 * 1024)
                    with counters_lock:
                        counters["success"] += 1
                    with manifest_lock:
                        manifest[video_id] = {
                            "status": "success",
                            "file": vid_target.name,
                            "type": "video",
                            "size_mb": round(size_mb, 2),
                            "title": title
                        }
                        save_manifest_atomic(manifest_path, manifest)
                    print(f"[{tag}]   -> 🎬✅ 视频下载成功: {vid_target.name} ({size_mb:.1f} MB)", flush=True)
                else:
                    with counters_lock:
                        counters["fail"] += 1
                    with manifest_lock:
                        manifest[video_id] = {"status": "failed", "reason": "视频流写入失败", "title": title}
                        save_manifest_atomic(manifest_path, manifest)
                    print(f"[{tag}]   -> ❌ 视频流写入失败", flush=True)

            task_queue.task_done()
            time.sleep(random.uniform(2.5, 3.5))

    finally:
        worker.close()
        print(f"[{tag}] 线程处理结束，资源已释放。", flush=True)


def download_single_table_multithreaded(
    excel_path: Path,
    dest_folder: Path,
    concurrency: int = 3,
    max_test: Optional[int] = None
):
    dest_folder.mkdir(parents=True, exist_ok=True)
    completion_flag = dest_folder / "_COMPLETED.flag"
    manifest_path = dest_folder / "manifest.json"

    if completion_flag.exists():
        print(f"\n⚡ 检测到 {dest_folder.name} 已经存在 _COMPLETED.flag，说明该表已完全下载完毕！", flush=True)
        return

    print(f"\n=======================================================")
    print(f"🎯 优先任务启动: 三进程协作下载单一总榜")
    print(f"源文件: {excel_path}")
    print(f"目标目录: {dest_folder}")
    print(f"并发工作线程: {concurrency}")
    print(f"=======================================================\n", flush=True)

    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"已加载历史 manifest 记录: {len(manifest)} 条")
        except Exception:
            manifest = {}

    df = pd.read_excel(excel_path)
    link_col = None
    for col in ["视频链接", "播放地址", "链接", "url", "URL"]:
        if col in df.columns:
            link_col = col
            break
    if not link_col:
        for col in df.columns:
            samples = df[col].dropna().astype(str).head(10)
            if any("douyin.com" in s for s in samples):
                link_col = col
                break

    if not link_col:
        raise ValueError(f"未在表格中找到视频链接列！表格列名: {df.columns.tolist()}")

    if max_test:
        df = df.head(max_test)

    tasks = []
    for idx, row in df.iterrows():
        rank = row.get("排名", idx + 1)
        raw_url = str(row.get(link_col, "")).strip()
        vid = bdh.extract_video_id(raw_url)
        title = str(row.get("视频标题", "")).strip()
        if vid:
            tasks.append((idx, rank, vid, raw_url, title))

    print(f"有效视频行数: {len(tasks)} 行待分配给 {concurrency} 个工作线程并发处理。\n", flush=True)

    manifest_lock = threading.Lock()
    counters = {"success": 0, "skip": 0, "deleted": 0, "fail": 0}
    counters_lock = threading.Lock()

    def run_pass(task_list, pass_name="首轮"):
        print(f"\n--- 启动【{pass_name}】并行下载（待处理 {len(task_list)} 项）---", flush=True)
        task_queue = queue.Queue()
        for t in task_list:
            task_queue.put(t)

        threads = []
        for i in range(1, concurrency + 1):
            t = threading.Thread(
                target=worker_thread,
                args=(
                    i,
                    task_queue,
                    len(tasks),
                    dest_folder,
                    manifest,
                    manifest_lock,
                    manifest_path,
                    counters,
                    counters_lock
                ),
                name=f"Worker-{i}"
            )
            threads.append(t)
            t.start()
            time.sleep(1.0)

        for t in threads:
            t.join()

    # 首轮完整执行
    run_pass(tasks, "首轮全量处理")

    # 查漏补缺轮：检查 manifest 中是否还有 status == 'failed' 的行
    failed_vids = set(vid for vid, info in manifest.items() if info.get("status") == "failed")
    if failed_vids:
        print(f"\n🔍 首轮结束，发现 {len(failed_vids)} 个视频因临时网络/风控失败，启动查漏补缺轮...", flush=True)
        # 清除 failed 标记以允许重新尝试
        with manifest_lock:
            for f_vid in failed_vids:
                manifest.pop(f_vid, None)
            save_manifest_atomic(manifest_path, manifest)

        retry_tasks = [t for t in tasks if t[2] in failed_vids]
        if retry_tasks:
            run_pass(retry_tasks, "第2轮查漏补缺")

    # 保存最终 manifest 并打标
    save_manifest_atomic(manifest_path, manifest)
    if not max_test:
        completion_flag.touch(exist_ok=True)
        print(f"完成标记: {completion_flag} 已生成")
    else:
        print(f"测试模式（--test {max_test}），不生成完成标记。")

    print(f"\n=======================================================")
    print(f"🎉 【{dest_folder.name}】全表下载与查漏补缺全部完成！")
    print(f"最终统计: 成功 {counters['success']} | 跳过 {counters['skip']} | 已删除 {counters['deleted']} | 失败 {counters['fail']}")
    print(f"=======================================================\n", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="优先下载指定表格，完成后自动恢复原 266 表批量任务")
    parser.add_argument("--test", type=int, default=None, help="仅测试前 N 个条目")
    parser.add_argument("--concurrency", type=int, default=3, help="并发线程数（默认 3）")
    parser.add_argument("--no-resume", action="store_true", help="完成后不自动恢复原 266 表任务")
    args = parser.parse_args()

    # 步骤 1: 优先执行星图总榜下载
    download_single_table_multithreaded(
        excel_path=TARGET_EXCEL,
        dest_folder=TARGET_DEST,
        concurrency=args.concurrency,
        max_test=args.test
    )

    # 步骤 2: 恢复原先的 266 个表格批量下载任务
    if not args.no_resume:
        print("\n" + "=" * 60)
        print("🚀 【优先级任务全部达成】立即无缝恢复原 266 个榜单批量轮询任务！")
        print("=" * 60 + "\n", flush=True)

        huitun_script = Path(__file__).parent / "batch_download_huitun.py"
        cmd = [sys.executable, str(huitun_script), "--concurrency", str(args.concurrency)]
        subprocess.run(cmd)


if __name__ == "__main__":
    main()
