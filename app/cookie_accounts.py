import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union
from uuid import uuid4

from .account_storage import account_storage_lock, deleted_marker_path
from .config import (
    BROWSER_ACCOUNTS_DIR,
    COOKIE_INVALIDATION_STRIKE_THRESHOLD,
    COOKIE_SWITCH_BATCH_THRESHOLD,
    PLAYWRIGHT_STORAGE_STATE_PATH,
    ensure_directories,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_account_id(label: str) -> str:
    normalized = re.sub(r'[\\/:*?"<>|]+', "-", str(label or "").strip())
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = normalized.strip("._-")
    return normalized or "cookie-account"


class AccountMetadataError(RuntimeError):
    """账号信息不可读；普通更新保留现场，显式重新登录才允许备份后重建。"""


@dataclass(frozen=True)
class CookieAccount:
    account_id: str
    label: str
    storage_state_path: Path
    metadata_path: Path
    invalid: bool = False
    invalid_reason: str = ""
    invalidated_at: str = ""
    warning: bool = False
    warning_reason: str = ""
    warned_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    last_success_at: str = ""
    challenge_strikes: int = 0
    last_issue_reason: str = ""
    last_issue_at: str = ""
    relogin_in_progress: bool = False
    source: str = "managed"
    cookie_count: int = 0
    has_state: bool = False
    has_usable_state: bool = False
    metadata_error: str = ""

    @property
    def status_text(self) -> str:
        if self.relogin_in_progress:
            return "重新登录中"
        if self.metadata_error:
            return "信息损坏"
        if self.invalid:
            return "已剔除"
        if self.warning:
            return "预警"
        if not self.has_state:
            return "未写入"
        if not self.has_usable_state:
            return "空会话"
        return "可用"

    def to_dict(self, *, is_current: bool = False, is_next: bool = False) -> Dict[str, object]:
        return {
            "account_id": self.account_id,
            "label": self.label,
            "storage_state_path": str(self.storage_state_path),
            "storage_state_name": self.storage_state_path.name,
            "invalid": self.invalid,
            "invalid_reason": self.invalid_reason,
            "invalidated_at": self.invalidated_at,
            "warning": self.warning,
            "warning_reason": self.warning_reason,
            "warned_at": self.warned_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "last_success_at": self.last_success_at,
            "challenge_strikes": self.challenge_strikes,
            "last_issue_reason": self.last_issue_reason,
            "last_issue_at": self.last_issue_at,
            "relogin_in_progress": self.relogin_in_progress,
            "source": self.source,
            "cookie_count": self.cookie_count,
            "has_state": self.has_state,
            "has_usable_state": self.has_usable_state,
            "metadata_error": self.metadata_error,
            "status_text": self.status_text,
            "is_current": is_current,
            "is_next": is_next,
        }


class CookieAccountManager:
    def __init__(
        self,
        *,
        accounts_dir: Path = BROWSER_ACCOUNTS_DIR,
        legacy_storage_state_path: Path = PLAYWRIGHT_STORAGE_STATE_PATH,
        switch_batch_threshold: int = COOKIE_SWITCH_BATCH_THRESHOLD,
        invalidation_strike_threshold: int = COOKIE_INVALIDATION_STRIKE_THRESHOLD,
    ) -> None:
        self.accounts_dir = Path(accounts_dir)
        self.legacy_storage_state_path = Path(legacy_storage_state_path)
        self.switch_batch_threshold = max(1, int(switch_batch_threshold))
        self.invalidation_strike_threshold = max(1, int(invalidation_strike_threshold))
        self._lock = threading.RLock()

    def ensure_directories(self) -> None:
        ensure_directories()
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

    def prepare_account(self, label: str) -> CookieAccount:
        self.ensure_directories()
        normalized_label = str(label or "").strip()
        if not normalized_label:
            raise RuntimeError("cookies 备注不能为空")

        with self._lock:
            existing = self._find_existing_by_label_locked(normalized_label)
            if existing is not None:
                return existing

            base_account_id = _sanitize_account_id(normalized_label)
            account_id = base_account_id
            suffix = 2
            while (
                self._metadata_path(account_id).exists()
                or self._state_path(account_id).exists()
                or deleted_marker_path(self._state_path(account_id)).exists()
            ):
                account_id = f"{base_account_id}-{suffix}"
                suffix += 1

            metadata = self._default_metadata(account_id=account_id, label=normalized_label)
            if not self._write_metadata(account_id, metadata):
                raise RuntimeError("该账号已删除，请重新新增账号")
            return self._load_account(account_id)

    def record_login(self, account_id: str, *, label: Optional[str] = None) -> CookieAccount:
        with self._lock:
            account = self._load_account(account_id)
            metadata = self._read_metadata_for_relogin(account)
            metadata["label"] = str(label or account.label).strip() or account.label
            metadata["updated_at"] = _utcnow_iso()
            metadata["invalid"] = False
            metadata["invalid_reason"] = ""
            metadata["invalidated_at"] = ""
            metadata["warning"] = False
            metadata["warning_reason"] = ""
            metadata["warned_at"] = ""
            metadata["challenge_strikes"] = 0
            metadata["last_issue_reason"] = ""
            metadata["last_issue_at"] = ""
            metadata["relogin_in_progress"] = False
            if not self._write_metadata(account_id, metadata):
                raise RuntimeError("该账号已删除，登录会话未保存")
            return self._load_account(account_id)

    def list_accounts(self, *, include_legacy: bool = False) -> List[CookieAccount]:
        self.ensure_directories()
        with self._lock:
            accounts: List[CookieAccount] = []
            for metadata_path in sorted(self.accounts_dir.glob("*.meta.json")):
                account_id = metadata_path.name[: -len(".meta.json")]
                state_path = self._state_path(account_id)
                if deleted_marker_path(state_path).exists():
                    continue
                if account_id == "__legacy_default__" and (
                    not include_legacy or state_path != self.legacy_storage_state_path
                ):
                    continue
                accounts.append(self._load_account(account_id))

            known_ids = {account.account_id for account in accounts}
            for state_path in sorted(self.accounts_dir.glob("*.json")):
                if state_path.name.endswith(".meta.json"):
                    continue
                # relogin 的中间产物（如 xxx.tmp.json）不是孤儿账号，不收养
                if ".tmp" in state_path.name:
                    continue
                account_id = state_path.stem
                if account_id in known_ids or deleted_marker_path(state_path).exists():
                    continue
                metadata = self._default_metadata(account_id=account_id, label=account_id)
                if not self._write_metadata(account_id, metadata):
                    continue
                accounts.append(self._load_account(account_id))

            if (
                include_legacy
                and self.legacy_storage_state_path.exists()
                and not deleted_marker_path(self.legacy_storage_state_path).exists()
            ):
                legacy_id = "__legacy_default__"
                if legacy_id not in known_ids:
                    if not self._metadata_path(legacy_id).exists():
                        metadata = self._default_metadata(account_id=legacy_id, label="默认会话", source="legacy")
                        self._write_metadata(legacy_id, metadata)
                    accounts.append(self._load_account(legacy_id))

            accounts = [
                account for account in accounts
                if not deleted_marker_path(account.storage_state_path).exists()
            ]
            accounts.sort(key=lambda account: (account.label.lower(), account.account_id.lower()))
            return accounts

    def list_active_accounts(self) -> List[CookieAccount]:
        return [
            account
            for account in self.list_accounts()
            if not account.invalid and not account.warning and account.has_usable_state
        ]

    def list_warning_accounts(self) -> List[CookieAccount]:
        return [
            account
            for account in self.list_accounts()
            if not account.invalid and account.warning and account.has_usable_state
        ]

    def list_available_accounts(self) -> List[CookieAccount]:
        return [
            account
            for account in self.list_accounts()
            if not account.invalid and account.has_usable_state
        ]

    def build_pool_snapshot(self, *, current_account_id: str = "") -> Dict[str, object]:
        accounts = self.list_accounts()
        current = next((account for account in accounts if account.account_id == current_account_id), None)
        next_account = self.select_next_available_account(current_account_id=current_account_id, allow_same=False)
        payload_accounts = [
            account.to_dict(
                is_current=bool(current and account.account_id == current.account_id),
                is_next=bool(next_account and account.account_id == next_account.account_id),
            )
            for account in accounts
        ]
        active_count = sum(not account.invalid and not account.warning and account.has_usable_state for account in accounts)
        warning_count = sum(not account.invalid and account.warning and account.has_usable_state for account in accounts)
        excluded_count = sum(account.invalid for account in accounts)
        usable_count = active_count + warning_count
        switchable_count = len(self.list_available_accounts())
        return {
            "switch_batch_threshold": self.switch_batch_threshold,
            "invalidation_strike_threshold": self.invalidation_strike_threshold,
            "current_account_id": current.account_id if current else "",
            "current_label": current.label if current else "",
            "next_account_id": next_account.account_id if next_account else "",
            "next_label": next_account.label if next_account else "",
            "active_count": active_count,
            "warning_count": warning_count,
            "excluded_count": excluded_count,
            "usable_count": usable_count,
            "switchable_count": switchable_count,
            "accounts": payload_accounts,
        }

    def get_account(self, account_id: str) -> Optional[CookieAccount]:
        if not account_id:
            return None
        with self._lock:
            metadata_path = self._metadata_path(account_id)
            state_path = self._state_path(account_id)
            if deleted_marker_path(state_path).exists():
                return None
            if not metadata_path.exists() and not state_path.exists():
                return None
            return self._load_account(account_id)

    def select_current_or_first_available_account(self, *, current_account_id: str = "") -> Optional[CookieAccount]:
        available = self.list_available_accounts()
        if not available:
            return None
        if current_account_id:
            for account in available:
                if account.account_id == current_account_id:
                    return account
        return available[0]

    def select_next_available_account(
        self,
        *,
        current_account_id: str = "",
        allow_same: bool = False,
    ) -> Optional[CookieAccount]:
        available = self.list_available_accounts()
        if not available:
            return None
        if not current_account_id:
            return available[0]

        for index, account in enumerate(available):
            if account.account_id != current_account_id:
                continue
            if len(available) == 1:
                return account if allow_same else None
            return available[(index + 1) % len(available)]

        return available[0]

    def mark_used(self, account_id: str) -> Optional[CookieAccount]:
        return self._update_account(
            account_id,
            {
                "last_used_at": _utcnow_iso(),
                "updated_at": _utcnow_iso(),
            },
        )

    def mark_success(self, account_id: str) -> Optional[CookieAccount]:
        now = _utcnow_iso()

        def _updates(metadata: Dict[str, object]) -> Dict[str, object]:
            updates = {
                "last_used_at": now,
                "last_success_at": now,
                "updated_at": now,
            }
            if not metadata.get("warning"):
                updates["challenge_strikes"] = 0
                updates["last_issue_reason"] = ""
                updates["last_issue_at"] = ""
            return updates

        return self._update_account(account_id, _updates)

    def note_issue(self, account_id: str, reason: str) -> Optional[CookieAccount]:
        now = _utcnow_iso()
        reason = str(reason or "").strip()

        def _updates(metadata: Dict[str, object]) -> Dict[str, object]:
            updates = {
                "last_used_at": now,
                "updated_at": now,
                "last_issue_reason": reason,
                "last_issue_at": now,
            }
            # 已预警账号只记录最近 issue，不再累加 strike，避免重复预警与计数失真
            if not metadata.get("warning"):
                updates["challenge_strikes"] = max(0, int(metadata.get("challenge_strikes") or 0)) + 1
            return updates

        return self._update_account(account_id, _updates)

    def mark_invalid(self, account_id: str, reason: str) -> Optional[CookieAccount]:
        now = _utcnow_iso()
        return self._update_account(
            account_id,
            {
                "updated_at": now,
                "invalid": True,
                "invalid_reason": str(reason or "").strip(),
                "invalidated_at": now,
                "warning": False,
                "warning_reason": "",
                "warned_at": "",
                "relogin_in_progress": False,
            },
        )

    def clear_invalid(self, account_id: str) -> Optional[CookieAccount]:
        return self._update_account(
            account_id,
            {
                "updated_at": _utcnow_iso(),
                "invalid": False,
                "invalid_reason": "",
                "invalidated_at": "",
                "challenge_strikes": 0,
            },
        )

    def mark_warning(self, account_id: str, reason: str) -> Optional[CookieAccount]:
        now = _utcnow_iso()
        return self._update_account(
            account_id,
            {
                "updated_at": now,
                "warning": True,
                "warning_reason": str(reason or "").strip(),
                "warned_at": now,
            },
        )

    def clear_warning(self, account_id: str) -> Optional[CookieAccount]:
        return self._update_account(
            account_id,
            {
                "updated_at": _utcnow_iso(),
                "warning": False,
                "warning_reason": "",
                "warned_at": "",
                "challenge_strikes": 0,
                "last_issue_reason": "",
                "last_issue_at": "",
            },
        )

    def delete_account(self, account_id: str) -> bool:
        """先阻断旧写者，再删除账号文件；标记保留，新增同名账号会分配新 ID。"""
        state_path = self._state_path(account_id)
        with self._lock, account_storage_lock(state_path):
            paths = [self._metadata_path(account_id), state_path]
            existed = any(path.exists() for path in paths)
            if not existed:
                return False
            deleted_marker_path(state_path).touch()
            for path in paths:
                path.unlink(missing_ok=True)
            return existed

    def reset_account_status(self, account_id: str) -> Optional[CookieAccount]:
        return self._update_account(
            account_id,
            {
                "updated_at": _utcnow_iso(),
                "invalid": False,
                "invalid_reason": "",
                "invalidated_at": "",
                "warning": False,
                "warning_reason": "",
                "warned_at": "",
                "challenge_strikes": 0,
                "last_issue_reason": "",
                "last_issue_at": "",
                "relogin_in_progress": False,
            },
        )

    def mark_relogin_in_progress(self, account_id: str, active: bool) -> Optional[CookieAccount]:
        return self._update_account(
            account_id,
            {
                "updated_at": _utcnow_iso(),
                "relogin_in_progress": bool(active),
            },
            repair_metadata=active,
        )

    def _default_metadata(self, *, account_id: str, label: str, source: str = "managed") -> Dict[str, object]:
        now = _utcnow_iso()
        return {
            "account_id": account_id,
            "label": label,
            "created_at": now,
            "updated_at": now,
            "last_used_at": "",
            "last_success_at": "",
            "invalid": False,
            "invalid_reason": "",
            "invalidated_at": "",
            "warning": False,
            "warning_reason": "",
            "warned_at": "",
            "challenge_strikes": 0,
            "last_issue_reason": "",
            "last_issue_at": "",
            "relogin_in_progress": False,
            "source": source,
        }

    def _metadata_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{account_id}.meta.json"

    def _state_path(self, account_id: str) -> Path:
        if account_id == "__legacy_default__":
            return self.legacy_storage_state_path
        return self.accounts_dir / f"{account_id}.json"

    def _find_existing_by_label_locked(self, label: str) -> Optional[CookieAccount]:
        normalized_label = str(label or "").strip()
        if not normalized_label:
            return None
        for account in self.list_accounts():
            if account.label == normalized_label:
                return account
        return None

    def _read_metadata(self, metadata_path: Path) -> Dict[str, object]:
        # 严格读取：文件存在但解析失败时抛异常，调用方不得用默认 metadata 覆盖损坏文件
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AccountMetadataError(f"账号 metadata 文件损坏，未做修改：{metadata_path}") from exc
        if not isinstance(payload, dict):
            raise AccountMetadataError(f"账号 metadata 内容非法，未做修改：{metadata_path}")
        try:
            int(payload.get("challenge_strikes") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AccountMetadataError(f"账号 metadata 计数非法，未做修改：{metadata_path}") from exc
        return payload

    def _read_metadata_for_relogin(self, account: CookieAccount) -> Dict[str, object]:
        try:
            return self._read_metadata(account.metadata_path)
        except AccountMetadataError:
            # 保留损坏原件用于排查；扫码成功前不把旧会话重新放回可用池。
            backup = account.metadata_path.with_name(
                f"{account.metadata_path.name}.{uuid4().hex}.corrupt"
            )
            shutil.copy2(account.metadata_path, backup)
            metadata = self._default_metadata(
                account_id=account.account_id, label=account.label, source=account.source
            )
            metadata.update(invalid=True, invalid_reason="账号信息损坏，等待重新登录修复")
            return metadata

    def _write_metadata(self, account_id: str, payload: Dict[str, object]) -> bool:
        with account_storage_lock(self._state_path(account_id)):
            if deleted_marker_path(self._state_path(account_id)).exists():
                return False
            self._write_metadata_unlocked(account_id, payload)
        return True

    def _write_metadata_unlocked(self, account_id: str, payload: Dict[str, object]) -> None:
        # tmp + os.replace 原子写，避免读-改-写过程中崩溃留下半截文件
        metadata_path = self._metadata_path(account_id)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = metadata_path.with_name(
            f"{metadata_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp_path, metadata_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _load_account(self, account_id: str) -> CookieAccount:
        metadata_path = self._metadata_path(account_id)
        state_path = self._state_path(account_id)
        metadata_error = ""
        try:
            metadata = self._read_metadata(metadata_path)
        except AccountMetadataError as exc:
            # 保留在页面中供重新登录修复，但隔离出可用池，避免未知风控状态被重置。
            metadata = {}
            metadata_error = str(exc)
        metadata = self._migrate_legacy_auto_invalid(account_id, metadata)
        label = str(metadata.get("label") or account_id).strip() or account_id
        cookie_count = self._cookie_count(state_path)
        has_state = state_path.exists()
        has_usable_state = cookie_count > 0
        source = str(metadata.get("source") or ("legacy" if account_id == "__legacy_default__" else "managed"))
        return CookieAccount(
            account_id=account_id,
            label=label,
            storage_state_path=state_path,
            metadata_path=metadata_path,
            invalid=bool(metadata_error or metadata.get("invalid")),
            invalid_reason=metadata_error or str(metadata.get("invalid_reason") or "").strip(),
            invalidated_at=str(metadata.get("invalidated_at") or "").strip(),
            warning=bool(metadata.get("warning")),
            warning_reason=str(metadata.get("warning_reason") or "").strip(),
            warned_at=str(metadata.get("warned_at") or "").strip(),
            created_at=str(metadata.get("created_at") or "").strip(),
            updated_at=str(metadata.get("updated_at") or "").strip(),
            last_used_at=str(metadata.get("last_used_at") or "").strip(),
            last_success_at=str(metadata.get("last_success_at") or "").strip(),
            challenge_strikes=max(0, int(metadata.get("challenge_strikes") or 0)),
            last_issue_reason=str(metadata.get("last_issue_reason") or "").strip(),
            last_issue_at=str(metadata.get("last_issue_at") or "").strip(),
            relogin_in_progress=bool(metadata.get("relogin_in_progress")),
            source=source,
            cookie_count=cookie_count,
            has_state=has_state,
            has_usable_state=has_usable_state,
            metadata_error=metadata_error,
        )

    def _migrate_legacy_auto_invalid(self, account_id: str, metadata: Dict[str, object]) -> Dict[str, object]:
        if not metadata:
            return metadata
        if not metadata.get("invalid"):
            return metadata
        if metadata.get("warning"):
            return metadata

        invalid_reason = str(metadata.get("invalid_reason") or "").strip()
        if not self._looks_like_legacy_auto_invalid_reason(invalid_reason):
            return metadata

        migrated = dict(metadata)
        migrated["invalid"] = False
        migrated["invalid_reason"] = ""
        migrated["invalidated_at"] = ""
        migrated["warning"] = True
        migrated["warning_reason"] = invalid_reason
        migrated["warned_at"] = str(metadata.get("invalidated_at") or metadata.get("updated_at") or _utcnow_iso())
        migrated["updated_at"] = _utcnow_iso()
        self._write_metadata(account_id, migrated)
        return migrated

    @staticmethod
    def _looks_like_legacy_auto_invalid_reason(reason: str) -> bool:
        normalized = str(reason or "").strip()
        return (
            "命中强风控/验证码" in normalized
            or "达到强信号切换阈值" in normalized
        )

    def _cookie_count(self, state_path: Path) -> int:
        if not state_path.exists():
            return 0
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(cookies, list):
            return 0
        return sum(1 for cookie in cookies if isinstance(cookie, dict) and str(cookie.get("name") or "").strip())

    def _update_account(
        self,
        account_id: str,
        updates: Union[Dict[str, object], Callable[[Dict[str, object]], Dict[str, object]]],
        *,
        repair_metadata: bool = False,
    ) -> Optional[CookieAccount]:
        with self._lock:
            account = self.get_account(account_id)
            if account is None:
                return None
            metadata = (
                self._read_metadata_for_relogin(account)
                if repair_metadata else self._read_metadata(account.metadata_path)
            )
            if not metadata:
                metadata = self._default_metadata(account_id=account.account_id, label=account.label, source=account.source)
            # callable 在锁内基于刚读到的 metadata 计算，消除 get→update 窗口（如 note_issue 丢 strike）
            if callable(updates):
                computed = updates(metadata)
                updates = dict(computed) if computed else {}
            metadata.update(updates)
            if not metadata.get("label"):
                metadata["label"] = account.label
            if not self._write_metadata(account_id, metadata):
                return None
            return self._load_account(account_id)
