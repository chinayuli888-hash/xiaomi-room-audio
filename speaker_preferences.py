"""Private per-device volume balance and participation, keyed by stable identity.

Constructing this class never creates or repairs a file. Invalid or unreadable
input raises PreferencesError so callers can block edits instead of overwriting
calibration. Setters change memory only; save() commits an atomic UTF-8 snapshot.
"""
from __future__ import annotations

import json
import math
from numbers import Real
import os
from pathlib import Path
import tempfile
import threading

MAX_RATIO = 1024.0


class PreferencesError(RuntimeError):
    """Loading or saving would risk losing the user's device preferences."""


def _ratio(value) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("音量比例必须是数字，不能使用布尔值。")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("音量比例必须为可表示的有限数值。") from error
    if not math.isfinite(result) or not 0 <= result <= MAX_RATIO:
        raise ValueError(f"音量比例必须在 0 至 {MAX_RATIO:g} 之间且为有限数值。")
    return result


def _selected(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("音箱参与状态必须为 true 或 false。")
    return value


def _object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("设备档案包含重复键，不能安全读取。")
        data[key] = value
    return data


def _constant(value):
    raise ValueError(f"设备档案包含非法 JSON 数值：{value}")


def _validate(profiles):
    if not isinstance(profiles, dict):
        raise ValueError("设备档案的顶层必须为对象。")
    if any(not isinstance(key, str) for key in profiles):
        raise ValueError("设备档案的键必须为字符串，不能隐式转换。")
    for record in profiles.values():
        # Preserve unknown metadata/record types without coercing them.
        if isinstance(record, dict):
            if "ratio" in record:
                _ratio(record["ratio"])
            if "selected" in record:
                _selected(record["selected"])


class SpeakerPreferences:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self._disk_contents = self._read_optional()
            self.profiles = ({} if self._disk_contents is None else
                             json.loads(self._disk_contents.decode("utf-8-sig"),
                                        object_pairs_hook=_object, parse_constant=_constant))
            _validate(self.profiles)
        except (OSError, UnicodeError, ValueError) as error:
            raise PreferencesError("无法安全读取音箱偏好；原文件未修改。请先修复设备档案。") from error

    def _read_optional(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _identifier(identifier):
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("设备身份必须为非空字符串。")
        return identifier

    def _record(self, identifier, *, create=False):
        identifier = self._identifier(identifier)
        if identifier not in self.profiles:
            if not create:
                return {}
            self.profiles[identifier] = {}
        record = self.profiles[identifier]
        if not isinstance(record, dict):
            if create:
                raise PreferencesError("该设备记录格式无法安全修改，原有内容已保留。")
            return {}
        return record

    def ratio(self, identifier) -> float:
        with self._lock:
            return _ratio(self._record(identifier).get("ratio", 1.0))

    def selected(self, identifier) -> bool:
        with self._lock:
            return _selected(self._record(identifier).get("selected", True))

    def set_ratio(self, identifier, ratio):
        value = _ratio(ratio)
        with self._lock:
            self._record(identifier, create=True)["ratio"] = value

    def set_selected(self, identifier, selected):
        value = _selected(selected)
        with self._lock:
            self._record(identifier, create=True)["selected"] = value

    def save(self):
        """Atomically commit; surface failures and preserve a changed disk file."""
        temporary = None
        with self._lock:
            try:
                _validate(self.profiles)
                text = json.dumps(self.profiles, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                if self._read_optional() != self._disk_contents:
                    raise PreferencesError("设备档案已被外部修改；为保护校准，本次未保存。请重新载入。")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 prefix=f".{self.path.name}.", suffix=".tmp",
                                                 dir=self.path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self._disk_contents = text.encode("utf-8")
            except (OSError, ValueError, TypeError, OverflowError) as error:
                raise PreferencesError("音箱偏好保存失败；请检查路径或文件权限后重试。") from error
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
