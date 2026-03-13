from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
TMP_DIR = PROJECT_ROOT / "tmp" / "spreadsheets"
UPLOAD_DIR = TMP_DIR / "uploads"
JOB_DIR = TMP_DIR / "jobs"
BROWSER_STATE_DIR = PROJECT_ROOT / "tmp" / "browser"
BROWSER_ACCOUNTS_DIR = BROWSER_STATE_DIR / "accounts"
OUTPUT_DIR = PROJECT_ROOT / "output" / "spreadsheet"
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"
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
ROW_MAX_RETRIES = 10
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
        STATIC_DIR,
        TEMPLATE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
