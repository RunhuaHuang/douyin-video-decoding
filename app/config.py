import os
import platform
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _path_from_env(name: str, default: Path) -> Path:
    """Return a user supplied path while keeping local defaults unchanged."""
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


def _int_from_env(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数，当前值: {value!r}") from exc


def _bool_from_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"环境变量 {name} 必须是布尔值，当前值: {value!r}")


APP_DIR = PROJECT_ROOT / "app"
TMP_DIR = PROJECT_ROOT / "tmp" / "spreadsheets"
UPLOAD_DIR = TMP_DIR / "uploads"
JOB_DIR = TMP_DIR / "jobs"
BROWSER_STATE_DIR = PROJECT_ROOT / "tmp" / "browser"
BROWSER_ACCOUNTS_DIR = BROWSER_STATE_DIR / "accounts"
OUTPUT_DIR = PROJECT_ROOT / "output" / "spreadsheet"
VIDEO_OUTPUT_DIR = _path_from_env("DOUYIN_VIDEO_OUTPUT_DIR", PROJECT_ROOT / "output" / "videos")
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# WebUI 默认只监听本机，NAS Compose 可通过环境变量改为监听容器网卡。
WEBUI_HOST = os.environ.get("DOUYIN_WEBUI_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEBUI_PORT = _int_from_env("DOUYIN_WEBUI_PORT", 8000)

DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"
MLX_SUPPORTED = sys.platform == "darwin" and platform.machine().lower() == "arm64"
DEFAULT_TRANSCRIPT_COLUMN = "视频文本"
STATUS_COLUMN = "处理状态"
ERROR_COLUMN = "失败原因"
ATTEMPTS_COLUMN = "尝试次数"
DEFAULT_DOWNLOAD_WORKERS = 2
DEFAULT_ASR_WORKERS = 1
MAX_DOWNLOAD_WORKERS = 8
MAX_ASR_WORKERS = 4
DEFAULT_BATCH_DELAY_SECONDS = 8
MAX_BATCH_DELAY_SECONDS = 30
ROW_RETRY_DELAY_SECONDS = 5
ROW_MAX_RETRIES = 3
PROCESS_BATCH_SIZE = 10
PROCESS_BATCH_DELAY_SECONDS = DEFAULT_BATCH_DELAY_SECONDS
DELETED_TRANSCRIPT_TEXT = "已删除"
ANTI_CRAWL_STREAK_THRESHOLD = 6
ANTI_CRAWL_COOLDOWN_SECONDS = 60 * 60
PROACTIVE_COOLDOWN_BATCH_THRESHOLD = 20
PROACTIVE_COOLDOWN_SECONDS = 30 * 60
DOUYIN_REQUEST_MIN_INTERVAL_SECONDS = 0.6
DOUYIN_REQUEST_MAX_INTERVAL_SECONDS = 1.4
DOUYIN_REQUEST_TIMEOUT_SECONDS = 25.0
DOUYIN_DOWNLOAD_TIMEOUT_SECONDS = 90.0
DOUYIN_BROWSER_FALLBACK_ENABLED = True
DOUYIN_BROWSER_FIRST = True
# NAS 无头环境需要先用无脚本请求识别验证码页；本机交互式浏览器默认关闭，
# 避免把可由 Chromium 解决的中间页误判成不可下载。
DOUYIN_PREFLIGHT_CHALLENGE = _bool_from_env("DOUYIN_PREFLIGHT_CHALLENGE", False)
DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS = 0.0
DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS = 0.0
PLAYWRIGHT_HEADLESS = True
PLAYWRIGHT_STORAGE_STATE_PATH = BROWSER_STATE_DIR / "douyin-storage-state.json"
COOKIE_SWITCH_BATCH_THRESHOLD = 30
COOKIE_INVALIDATION_STRIKE_THRESHOLD = 6
COOKIE_STRONG_SIGNAL_SWITCH_THRESHOLD = 6
COOKIE_RETRY_STRONG_SIGNAL_SWITCH_THRESHOLD = 20
PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = 15000
PLAYWRIGHT_NETWORK_IDLE_TIMEOUT_MS = 4000
PLAYWRIGHT_VIDEO_WAIT_TIMEOUT_MS = 3000
PLAYWRIGHT_AFTER_LOAD_WAIT_MS = 1200
PLAYWRIGHT_AFTER_PLAY_WAIT_MS = 1500
# 浏览器 APIRequestContext 整文件下载超时（覆盖响应体读取），不能沿用 15s 导航超时
PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS = int(DOUYIN_DOWNLOAD_TIMEOUT_SECONDS * 1000)
# 分流视频的音频流请求可能滞后于视频流，发现 media-video 特征后的有界等待宽限
PLAYWRIGHT_DASH_AUDIO_GRACE_MS = 1500
PLAYWRIGHT_INSTALL_HINT = "uv run playwright install chromium"
MODEL_PRESETS = [
    {
        "label": "Turbo",
        "models": ["mlx-community/whisper-large-v3-turbo"],
        "note": "速度优先。",
    },
    {
        "label": "Turbo + Large V3",
        "models": [
            "mlx-community/whisper-large-v3-turbo",
            "mlx-community/whisper-large-v3-mlx",
        ],
        "note": "最适合比较中文质量与速度。",
    },
    {
        "label": "Large V3",
        "models": ["mlx-community/whisper-large-v3-mlx"],
        "note": "通常比 Turbo 更稳，但更慢。",
    },
    {
        "label": "SenseVoice",
        "models": ["FunAudioLLM/SenseVoiceSmall"],
        "note": "当前默认，中文口语通常更自然，首次运行会额外准备 VAD 模型。",
    },
    {
        "label": "Large V3 + SenseVoice",
        "models": [
            "mlx-community/whisper-large-v3-mlx",
            "FunAudioLLM/SenseVoiceSmall",
        ],
        "note": "推荐的中文质量对比组合。",
    },
    {
        "label": "Small",
        "models": ["mlx-community/whisper-small-mlx"],
        "note": "体积更小，适合快速试跑。",
    },
]
if not MLX_SUPPORTED:
    MODEL_PRESETS = [
        preset for preset in MODEL_PRESETS
        if all("sensevoice" in model.lower() for model in preset["models"])
    ]

CSV_ENCODINGS = [
    "utf-8-sig",
    "utf-8",
    "gb18030",
    "gbk",
    "big5",
]


def ensure_directories() -> None:
    for path in (
        TMP_DIR,
        UPLOAD_DIR,
        JOB_DIR,
        BROWSER_STATE_DIR,
        BROWSER_ACCOUNTS_DIR,
        OUTPUT_DIR,
        VIDEO_OUTPUT_DIR,
        STATIC_DIR,
        TEMPLATE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
