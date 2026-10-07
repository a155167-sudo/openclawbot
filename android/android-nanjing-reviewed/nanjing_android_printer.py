#!/usr/bin/env python3
"""南京店 Android 出單器：由已發布 backend dispatch contract 自動綁定。

預設 dry-run。只有 --print 才會寫 Sheet/開 printer socket。TCP sendall 成功
只記為「已送印」，不宣稱實體出紙。正式 bearer/key 只由 Android 本機私有檔載入。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

APP_VERSION = "nanjing-backend-bound-printer-v2"
FORMAL_ORIGIN = "https://openclawbot-production-production.up.railway.app"
CONTRACT_PATH = "/internal/printer/v1/dispatch-contract"
CLAIM_PATH = "/internal/printer/v1/dispatch-claims"
RESULT_PATH = "/internal/printer/v1/dispatch-results"
DEFAULT_PRINTER_IP = "172.19.0.232"
DEFAULT_PRINTER_PORT = 9100
DEFAULT_WRITEBACK = "已送印"
TAIPEI = ZoneInfo("Asia/Taipei")
ORIGINAL_HEADERS = (
    "實際日期", "週期與星期", "午餐安排", "午餐熱量", "午餐蛋白", "晚餐安排",
    "晚餐熱量", "晚餐蛋白", "今日排餐總熱量", "今日排餐總蛋白",
    "熱量剩餘 / 蛋白質需補", "單日金額", "明日預定課表", "列印狀態",
)
DISPATCH_HEADERS = ("Dispatch_Row_ID", "Order_ID", "Menu_Version")
TAGGED_HEADERS = ORIGINAL_HEADERS + DISPATCH_HEADERS
PRINT_STATUS_POLICY = "external_mutable_not_publication_identity"
PENDING_STATUS = "待列印"
PRINTED_STATUSES = frozenset(("已送印", "已列印"))
ABSENT_MEALS = frozenset(("", "無", "無餐", "不供餐", "不出餐", "N/A", "NA", "—", "-"))
KEY_CANDIDATES = (
    "google_key.json", "/storage/emulated/0/Download/google_key.json",
    "/sdcard/Download/google_key.json",
)
MAX_RESPONSE_BYTES = 2_000_000


class ContractError(ValueError):
    """任何無法完整證明的 binding 都整批 fail closed。"""


class PreSendError(OSError):
    """connect 尚未完成，payload 確定未交給 sendall。"""


class AmbiguousSendError(OSError):
    """sendall 已開始，禁止盲目自動重印。"""


class ClaimRejected(ContractError):
    """Backend did not grant fresh send authority; external I/O is forbidden."""


def normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _valid_uid(value: str) -> bool:
    return len(value) == 33 and value.startswith("U") and all(c in "0123456789abcdefABCDEF" for c in value[1:])


def _aware_datetime(value: Any, context: str) -> str:
    raw = normalize_text(value)
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{context} 必須是含時區 ISO timestamp") from exc
    return raw


def _iso_date(value: Any, context: str) -> str:
    raw = normalize_text(value).replace("/", "-")
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ContractError(f"{context} 日期無效") from exc


def is_absent_meal(value: Any) -> bool:
    return normalize_text(value).upper() in ABSENT_MEALS


@dataclass(frozen=True)
class DeviceConfig:
    backend_origin: str
    device_bearer_token: str = field(repr=False)
    store_id: str
    workbook_id: str
    http_timeout_seconds: float = 8.0
    dispatch_mode: str = "legacy"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DeviceConfig":
        if not isinstance(raw, Mapping):
            raise ContractError("private config 必須是 JSON object")
        origin = normalize_text(raw.get("backend_origin")).rstrip("/")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment
                or origin != f"https://{parsed.netloc}" or origin != FORMAL_ORIGIN):
            raise ContractError("backend_origin 必須是無 path/credential 的 exact HTTPS origin")
        token = str(raw.get("device_bearer_token") or "").strip()
        placeholders = {"REPLACE_WITH_DEVICE_TOKEN", "CHANGE_ME", "TODO"}
        if len(token.encode("utf-8")) < 32 or token in placeholders:
            raise ContractError("尚未配置正式獨立 device bearer credential")
        store = normalize_text(raw.get("store_id"))
        workbook = normalize_text(raw.get("workbook_id"))
        if store != "nanjing" or not workbook:
            raise ContractError("store/workbook 必須明確綁定南京正式設定")
        try:
            timeout = float(raw.get("http_timeout_seconds", 8.0))
        except (TypeError, ValueError) as exc:
            raise ContractError("HTTP timeout 無效") from exc
        if not 1 <= timeout <= 30:
            raise ContractError("HTTP timeout 必須介於 1 到 30 秒")
        mode = normalize_text(raw.get("dispatch_mode") or "legacy").lower()
        if mode not in {"legacy", "backend_v2"}:
            raise ContractError("dispatch_mode 必須是 legacy 或 backend_v2")
        return cls(origin, token, store, workbook, timeout, mode)


def load_device_config(path: str) -> DeviceConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("無法讀取 Android 本機 private config") from exc
    return DeviceConfig.from_mapping(raw)


class RedirectBlocked(HTTPRedirectHandler):
    """Bearer endpoint 不接受 redirect，避免 token 被轉送到不同 origin。"""
    def __init__(self, expected_origin: str):
        super().__init__()
        self.expected_origin = expected_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urlsplit(newurl)
        new_origin = f"{new.scheme}://{new.netloc}"
        if new_origin != self.expected_origin:
            raise ContractError("backend cross-origin redirect 已阻擋")
        raise ContractError("backend redirect 已阻擋；請使用 canonical endpoint")


def fetch_dispatch_contract(config: DeviceConfig, service_date: date, *, opener=None) -> Mapping[str, Any]:
    query = urlencode({"store_id": config.store_id, "service_date": service_date.isoformat()})
    url = config.backend_origin + CONTRACT_PATH + "?" + query
    request = Request(url, headers={
        "Authorization": "Bearer " + config.device_bearer_token,
        "Accept": "application/json",
        "User-Agent": APP_VERSION,
    }, method="GET")
    opener = opener or build_opener(RedirectBlocked(config.backend_origin))
    try:
        with opener.open(request, timeout=config.http_timeout_seconds) as response:
            if getattr(response, "status", 200) != 200:
                raise ContractError("backend contract HTTP 狀態非 200")
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ContractError("backend contract response 過大")
        decoded = json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ContractError(f"backend device authentication failed (HTTP {exc.code})") from None
        raise ContractError(f"backend contract HTTP failure ({exc.code})") from None
    except (URLError, TimeoutError, OSError):
        raise ContractError("backend contract timeout/network failure") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError("backend contract JSON 無效") from None
    if not isinstance(decoded, Mapping):
        raise ContractError("backend contract response 必須是 object")
    return decoded


class BackendClaimClient:
    """Strict HTTPS claim/result client; bearer is header-only and redirects are denied."""
    def __init__(self, config: DeviceConfig, *, request_sender=None, opener=None):
        self.config = config
        self.request_sender = request_sender
        self.opener = opener or build_opener(RedirectBlocked(config.backend_origin))

    def _post(self, path: str, body: Mapping[str, str]) -> tuple[int, Mapping[str, Any]]:
        if self.request_sender is not None:
            try:
                status, decoded = self.request_sender("POST", path, body, self.config)
            except (TimeoutError, OSError, URLError):
                raise ContractError("backend claim/result timeout/network failure") from None
            if not isinstance(decoded, Mapping):
                raise ContractError("backend claim/result JSON 無效")
            return int(status), decoded
        request = Request(
            self.config.backend_origin + path,
            data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.config.device_bearer_token,
                "Accept": "application/json", "Content-Type": "application/json",
                "User-Agent": APP_VERSION,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.config.http_timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                raw = b"{}"
        except (URLError, TimeoutError, OSError):
            raise ContractError("backend claim/result timeout/network failure") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ContractError("backend claim/result response 過大")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ContractError("backend claim/result JSON 無效") from None
        if not isinstance(decoded, Mapping):
            raise ContractError("backend claim/result response 必須是 object")
        return status, decoded

    def claim(self, *, operation_id: str, row: "ScheduleRow") -> str:
        status, data = self._post(CLAIM_PATH, {
            "operation_id": operation_id,
            "dispatch_row_id": row.dispatch_row_id,
            "service_date": _iso_date(row.service_date, "claim service_date"),
        })
        if status == 409:
            raise ClaimRejected("backend claim rejected or replayed")
        expected = {
            "status", "operation_id", "dispatch_row_id", "service_date", "send_permit",
        }
        if (status != 201 or set(data) != expected or data.get("status") != "send_permitted"
                or data.get("operation_id") != operation_id
                or data.get("dispatch_row_id") != row.dispatch_row_id
                or data.get("service_date") != _iso_date(row.service_date, "claim service_date")
                or not isinstance(data.get("send_permit"), str) or not data["send_permit"]):
            raise ContractError("backend claim response binding 無效")
        return str(data["send_permit"])

    def report(self, *, operation_id: str, send_permit: str, outcome: str,
               ack_id: str, evidence_digest: str) -> None:
        status, data = self._post(RESULT_PATH, {
            "operation_id": operation_id, "send_permit": send_permit,
            "outcome": outcome, "ack_id": ack_id, "evidence_digest": evidence_digest,
        })
        if (status != 200 or set(data) != {"operation_id", "status", "recorded_at", "printed"}
                or data.get("operation_id") != operation_id or data.get("status") != outcome
                or data.get("printed") is not False):
            raise ContractError("backend result acknowledgement binding 無效")


@dataclass(frozen=True)
class ContractRow:
    dispatch_row_id: str
    worksheet_id: int
    worksheet_title: str
    order_id: str
    order_status: str
    customer_uid: str
    formalized_at: str
    menu_version: str
    service_date: str
    lunch: str
    dinner: str
    source_columns: tuple[str, ...]
    publication_print_status: str
    print_status_policy: str
    receipt_version: int


def parse_contract(raw: Mapping[str, Any], config: DeviceConfig, target: date) -> tuple[ContractRow, ...]:
    if not isinstance(raw, Mapping):
        raise ContractError("backend contract response 必須是 object")
    if set(raw) != {"contract_version", "store_id", "workbook_id", "generated_at", "rows"}:
        raise ContractError("backend contract top-level shape 不符合 v1")
    if raw.get("contract_version") != 1 or raw.get("store_id") != config.store_id:
        raise ContractError("backend contract version/store 不一致")
    if raw.get("workbook_id") != config.workbook_id:
        raise ContractError("backend export workbook 與 local config 不一致")
    _aware_datetime(raw.get("generated_at"), "generated_at")
    records = raw.get("rows")
    if not isinstance(records, list):
        raise ContractError("backend rows 必須是 array")
    result: list[ContractRow] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        context = f"backend row {index + 1}"
        if not isinstance(item, Mapping):
            raise ContractError(f"{context} 必須是 object")
        if set(item) != {
            "dispatch_row_id", "worksheet_id", "worksheet_title", "order_id",
            "order_status", "customer_uid", "formalized_at", "menu_version",
            "service_date", "lunch", "dinner", "source_columns",
            "publication_print_status", "print_status_policy", "receipt_version",
        }:
            raise ContractError(f"{context} shape 不符合 v1")
        dispatch_id = normalize_text(item.get("dispatch_row_id"))
        if not dispatch_id or dispatch_id in seen:
            raise ContractError("backend dispatch_row_id 缺失或重複")
        seen.add(dispatch_id)
        worksheet_id = item.get("worksheet_id")
        if isinstance(worksheet_id, bool) or not isinstance(worksheet_id, int) or worksheet_id < 0:
            raise ContractError(f"{context} worksheet numeric ID 無效")
        worksheet_title = normalize_text(item.get("worksheet_title"))
        order_id_raw = item.get("order_id")
        if isinstance(order_id_raw, bool) or not isinstance(order_id_raw, int) or order_id_raw < 1:
            raise ContractError(f"{context} order ID 無效")
        order_status = normalize_text(item.get("order_status"))
        uid = normalize_text(item.get("customer_uid"))
        version = normalize_text(item.get("menu_version"))
        service = _iso_date(item.get("service_date"), f"{context} service_date")
        source_raw = item.get("source_columns")
        if not isinstance(source_raw, list) or len(source_raw) != 14:
            raise ContractError(f"{context} 必須含完整 14 欄 source_columns")
        source = tuple(normalize_text(value) for value in source_raw)
        lunch, dinner = normalize_text(item.get("lunch")), normalize_text(item.get("dinner"))
        publication_status = normalize_text(item.get("publication_print_status"))
        policy = normalize_text(item.get("print_status_policy"))
        if (not worksheet_title or order_status != "activated" or not _valid_uid(uid) or not version
                or service != target.isoformat() or _iso_date(source[0], f"{context} source date") != service
                or lunch != source[2] or dinner != source[5]
                or publication_status != source[13] or policy != PRINT_STATUS_POLICY
                or item.get("receipt_version") != 1):
            raise ContractError(f"{context} publication/identity binding 無效")
        result.append(ContractRow(
            dispatch_id, worksheet_id, worksheet_title, str(order_id_raw), order_status, uid,
            _aware_datetime(item.get("formalized_at"), f"{context} formalized_at"), version,
            service, lunch, dinner, source, publication_status, policy, 1,
        ))
    return tuple(result)


@dataclass(frozen=True)
class ScheduleRow:
    worksheet: Any = field(compare=False, repr=False)
    dispatch_row_id: str
    worksheet_title: str
    worksheet_id: int
    row_number: int
    customer_uid: str
    customer_name: str
    order_id: str
    schedule_version: str
    service_date: str
    lunch: str
    dinner: str
    status_column: int
    formalized_at: str
    source_columns: tuple[str, ...]

    @property
    def semantic_identity(self) -> str:
        return self.dispatch_row_id

    @property
    def dispatch_key(self) -> str:
        values = (
            self.dispatch_row_id, self.customer_uid, self.order_id, self.schedule_version,
            self.worksheet_title, str(self.worksheet_id), self.service_date, self.formalized_at,
            *self.source_columns[:13], APP_VERSION,
        )
        return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Reconciliation:
    rows: tuple[ScheduleRow, ...]
    already_printed: int = 0


def _customer_name(title: str) -> str:
    return title.split("_", 1)[0].strip() or "顧客"


def reconcile_contract(workbook: Any, records: Sequence[ContractRow], config: DeviceConfig, target: date) -> Reconciliation:
    if normalize_text(getattr(workbook, "id", "")) != config.workbook_id:
        raise ContractError("opened Sheet workbook 與 export/config 不一致")
    worksheets = list(workbook.worksheets())
    by_identity: dict[tuple[int, str], Any] = {}
    for ws in worksheets:
        key = (getattr(ws, "id", None), str(getattr(ws, "title", "")))
        if key in by_identity:
            raise ContractError("Google workbook 有重複 worksheet identity")
        by_identity[key] = ws
    grouped: dict[tuple[int, str], list[ContractRow]] = {}
    for record in records:
        grouped.setdefault((record.worksheet_id, record.worksheet_title), []).append(record)
    selected: list[ScheduleRow] = []
    already = 0
    for identity, expected in grouped.items():
        ws = by_identity.get(identity)
        if ws is None:
            raise ContractError("backend 指定 worksheet numeric ID/title 不存在")
        values = ws.get_all_values()
        header_positions = [i for i, row in enumerate(values[:15]) if tuple(normalize_text(c) for c in row) == TAGGED_HEADERS]
        if len(header_positions) != 1:
            raise ContractError(f"{identity[1]}: exact 17 欄 header 必須唯一")
        header_index = header_positions[0]
        found: dict[str, tuple[int, list[str]]] = {}
        for zero_index, raw_row in enumerate(values[header_index + 1:], start=header_index + 1):
            row = [normalize_text(c) for c in raw_row]
            if not any(row):
                continue
            if len(row) != 17:
                # A populated schedule-shaped row may not evade reconciliation by
                # truncation or by smuggling uncontracted columns.
                if row and _iso_date(row[0], f"{identity[1]} row {zero_index + 1}") == target.isoformat():
                    raise ContractError(f"{identity[1]}: target row 必須是完整且精確的 17 欄 identity")
                continue
            if _iso_date(row[0], f"{identity[1]} row {zero_index + 1}") != target.isoformat():
                continue
            dispatch_id = row[14]
            if not dispatch_id or dispatch_id in found:
                raise ContractError(f"{identity[1]}: target dispatch row identity 缺失或重複")
            found[dispatch_id] = (zero_index + 1, row)
        expected_ids = {record.dispatch_row_id for record in expected}
        if set(found) != expected_ids:
            raise ContractError(f"{identity[1]}: missing row 或 unknown receipt row")
        for record in expected:
            row_number, current = found[record.dispatch_row_id]
            if current[15] != record.order_id or current[16] != record.menu_version:
                raise ContractError(f"{identity[1]}: order/version tag 不一致")
            # 列印狀態是 external mutable；其他 13 欄必須和 publication source 完全一致。
            if tuple(current[:13]) != record.source_columns[:13]:
                raise ContractError(f"{identity[1]}: current Sheet source payload tampered")
            status = current[13]
            if status in PRINTED_STATUSES:
                already += 1
                continue
            if status != PENDING_STATUS:
                raise ContractError(f"{identity[1]}: 未知列印狀態")
            selected.append(ScheduleRow(
                ws, record.dispatch_row_id, record.worksheet_title, record.worksheet_id,
                row_number, record.customer_uid, _customer_name(record.worksheet_title),
                record.order_id, record.menu_version, target.strftime("%Y/%m/%d"),
                record.lunch, record.dinner, 14, record.formalized_at, record.source_columns,
            ))
    # An empty/partial backend export must not let a tagged target-date row on an
    # otherwise unreferenced worksheet bypass the trusted receipt set.
    for identity, ws in by_identity.items():
        if identity in grouped:
            continue
        values = ws.get_all_values()
        header_positions = [i for i, row in enumerate(values[:15]) if tuple(normalize_text(c) for c in row) == TAGGED_HEADERS]
        if len(header_positions) > 1:
            raise ContractError(f"{identity[1]}: exact 17 欄 header 必須唯一")
        if not header_positions:
            continue
        header_index = header_positions[0]
        for zero_index, raw_row in enumerate(values[header_index + 1:], start=header_index + 1):
            row = [normalize_text(c) for c in raw_row]
            if not any(row):
                continue
            if len(row) != 17:
                if row and _iso_date(row[0], f"{identity[1]} row {zero_index + 1}") == target.isoformat():
                    raise ContractError(f"{identity[1]}: target row 必須是完整且精確的 17 欄 identity")
                continue
            if _iso_date(row[0], f"{identity[1]} row {zero_index + 1}") == target.isoformat() and row[14]:
                raise ContractError(f"{identity[1]}: unknown receipt row")
    keys = [row.dispatch_key for row in selected]
    if len(keys) != len(set(keys)):
        raise ContractError("跨 worksheet 出現重複 dispatch identity")
    return Reconciliation(tuple(selected), already)


def contract_rows_to_schedule(records: Sequence[ContractRow]) -> tuple[ScheduleRow, ...]:
    """Build v2 candidates only from the authenticated authority response, never Sheet cache."""
    return tuple(ScheduleRow(
        None, record.dispatch_row_id, record.worksheet_title, record.worksheet_id, 0,
        record.customer_uid, _customer_name(record.worksheet_title), record.order_id,
        record.menu_version, record.service_date.replace("-", "/"), record.lunch,
        record.dinner, 0, record.formalized_at, record.source_columns,
    ) for record in records)


@dataclass(frozen=True)
class EncodedText:
    payload: bytes
    unsupported_characters: tuple[str, ...]


@dataclass(frozen=True)
class RenderedTicket:
    payload: bytes
    unsupported_characters: tuple[str, ...]


def encode_cp950_visible(text: str) -> EncodedText:
    unsupported: list[str] = []
    for char in text:
        try:
            char.encode("cp950")
        except UnicodeEncodeError:
            if char not in unsupported:
                unsupported.append(char)
    return EncodedText(text.encode("cp950", errors="replace"), tuple(unsupported))


def _render_lines(lines: Sequence[tuple[bytes, str]]) -> RenderedTicket:
    payload = bytearray(b"\x1b\x40\x1c\x26\x1c\x43\x01")
    unsupported: list[str] = []
    for mode, text in lines:
        encoded = encode_cp950_visible(text)
        payload.extend(mode); payload.extend(encoded.payload)
        for char in encoded.unsupported_characters:
            if char not in unsupported:
                unsupported.append(char)
    payload.extend(b"\x1b\x21\x00\n\n\n\x1d\x56\x00")
    return RenderedTicket(bytes(payload), tuple(unsupported))


def render_customer_ticket(customer_name: str, service_date: str, lunch: str, dinner: str) -> RenderedTicket:
    big, tall, normal = b"\x1b\x21\x30", b"\x1b\x21\x10", b"\x1b\x21\x00"
    return _render_lines(((big, "一日樂食 南京店\n"), (tall, "Deli Express\n================\n"),
        (tall, f"客戶：{customer_name}\n日期：{service_date}\n----------------\n"),
        (big, "午餐：\n"), (tall, f"{lunch or '無'}\n"), (big, "晚餐：\n"),
        (tall, f"{dinner or '無'}\n"), (normal, "================\n")))


def render_summary(service_date: str, lunch: Mapping[str, int], dinner: Mapping[str, int]) -> RenderedTicket:
    big, tall = b"\x1b\x21\x30", b"\x1b\x21\x10"
    lines: list[tuple[bytes, str]] = [(big, "內場備餐總單\n"), (tall, f"日期：{service_date}\n================\n")]
    for label, items in (("午餐總計：", lunch), ("晚餐總計：", dinner)):
        if items:
            lines.append((big, label + "\n"))
            for name, count in sorted(items.items()):
                lines.append((tall, f"{name}\n數量：{count}份\n----------------\n"))
    return _render_lines(lines)


class SocketTransport:
    def __init__(self, host: str, port: int, *, timeout: float = 8.0, socket_factory: Callable[[], Any] | None = None):
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self.socket_factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))

    def send(self, payload: bytes) -> int:
        sock = self.socket_factory()
        try:
            sock.settimeout(self.timeout)
            try: sock.connect((self.host, self.port))
            except OSError as exc: raise PreSendError("printer connect failed") from exc
            try: sock.sendall(payload)
            except OSError as exc: raise AmbiguousSendError("printer send state ambiguous") from exc
            return len(payload)
        finally:
            try: sock.close()
            except Exception: pass


class Ledger:
    """本機 durable ledger；不保存 bearer、Google key 或完整 export JSON。"""
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS customer_dispatch(
              dispatch_key TEXT PRIMARY KEY, semantic_identity TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL, worksheet_title TEXT NOT NULL, worksheet_id INTEGER NOT NULL,
              row_number INTEGER NOT NULL, customer_uid TEXT NOT NULL, customer_name TEXT NOT NULL,
              order_id TEXT NOT NULL, schedule_version TEXT NOT NULL, service_date TEXT NOT NULL,
              lunch TEXT NOT NULL, dinner TEXT NOT NULL, status_column INTEGER NOT NULL,
              last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
              claim_token TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS summary_dispatch(
              summary_key TEXT PRIMARY KEY, service_date TEXT NOT NULL, state TEXT NOT NULL,
              member_keys_json TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
              claim_token TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS summary_members(
              dispatch_key TEXT PRIMARY KEY, summary_key TEXT NOT NULL, summarized_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS backend_v2_dispatch(
              operation_id TEXT PRIMARY KEY, dispatch_key TEXT NOT NULL UNIQUE,
              send_permit TEXT NOT NULL, state TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT '', ack_id TEXT NOT NULL DEFAULT '',
              evidence_digest TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            """)
            # Additive migration keeps old ambiguous/manual evidence intact.  Empty
            # tokens remain valid only for writeback recovery; they never authorize
            # a retryable -> sending transition.
            for table in ("customer_dispatch", "summary_dispatch"):
                columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                if "claim_token" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise ContractError("無法限制 local ledger 權限") from exc

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=0.1); conn.row_factory = sqlite3.Row; return conn

    @staticmethod
    def _now(): return datetime.now(TAIPEI).isoformat(timespec="seconds")

    def states(self):
        with self._connect() as conn:
            return [(str(r[0]), str(r[1])) for r in conn.execute("SELECT state,dispatch_key FROM customer_dispatch ORDER BY dispatch_key")]

    def v2_state(self, operation_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT state FROM backend_v2_dispatch WHERE operation_id=?", (operation_id,)).fetchone()
            return str(row[0]) if row else None

    def v2_record(self, operation_id: str):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM backend_v2_dispatch WHERE operation_id=?", (operation_id,)).fetchone()

    def store_v2_permit(self, operation_id: str, dispatch_key: str, permit: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO backend_v2_dispatch
              (operation_id,dispatch_key,send_permit,state,updated_at)
              VALUES(?,?,?,'permitted',?)""", (operation_id, dispatch_key, permit, self._now()))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback(); raise ContractError("local v2 permit replay/conflict; refusing send") from exc
        finally:
            conn.close()

    def claim_v2_send(self, operation_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""UPDATE backend_v2_dispatch SET state='sending',updated_at=?
              WHERE operation_id=? AND state='permitted'""", (self._now(), operation_id))
            if changed.rowcount != 1:
                raise ContractError("local v2 send claim unavailable; refusing send")
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def mark_v2_report(self, operation_id: str, outcome: str, ack_id: str, evidence: str) -> None:
        with self._connect() as conn:
            changed = conn.execute("""UPDATE backend_v2_dispatch
              SET state='pending_report',outcome=?,ack_id=?,evidence_digest=?,updated_at=?
              WHERE operation_id=? AND state='sending'""",
              (outcome, ack_id, evidence, self._now(), operation_id))
            if changed.rowcount != 1:
                raise ContractError("stale local v2 completion refused")

    def complete_v2_report(self, operation_id: str) -> None:
        with self._connect() as conn:
            changed = conn.execute("""UPDATE backend_v2_dispatch SET state='complete',updated_at=?
              WHERE operation_id=? AND state='pending_report'""", (self._now(), operation_id))
            if changed.rowcount != 1:
                raise ContractError("stale local v2 report acknowledgement refused")

    def pending_v2_reports(self):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM backend_v2_dispatch WHERE state='pending_report' ORDER BY operation_id").fetchall()

    @staticmethod
    def _busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "locked" in message or "busy" in message

    @dataclass(frozen=True)
    class Claim:
        action: str
        token: str = ""

    def claim_customer_send(self, row: ScheduleRow) -> "Ledger.Claim":
        now = self._now()
        token = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT state,claim_token FROM customer_dispatch WHERE dispatch_key=?", (row.dispatch_key,)).fetchone()
            if existing:
                state = str(existing[0])
                if state == "retryable":
                    changed = conn.execute("""UPDATE customer_dispatch
                      SET state='sending',claim_token=?,last_error='',updated_at=?
                      WHERE dispatch_key=? AND state='retryable'""", (token, now, row.dispatch_key))
                    if changed.rowcount != 1:
                        raise ContractError("ledger retry claim lost; refusing printer send")
                    conn.commit(); return self.Claim("send", token)
                if state == "sending":
                    conn.commit(); return self.Claim("manual")
                if state == "sent_pending_writeback":
                    conn.commit(); return self.Claim("writeback", str(existing[1]))
                conn.commit(); return self.Claim("complete" if state == "complete" else "manual")
            conn.execute("""INSERT INTO customer_dispatch(
              dispatch_key,semantic_identity,state,worksheet_title,worksheet_id,row_number,
              customer_uid,customer_name,order_id,schedule_version,service_date,lunch,dinner,
              status_column,last_error,updated_at,claim_token)
              VALUES(?,?,'sending',?,?,?,?,?,?,?,?,?,?,?,'',?,?)""", (
                row.dispatch_key, row.semantic_identity, row.worksheet_title, row.worksheet_id, row.row_number,
                row.customer_uid, row.customer_name, row.order_id, row.schedule_version, row.service_date,
                row.lunch, row.dinner, row.status_column, now, token))
            conn.commit(); return self.Claim("send", token)
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if self._busy(exc):
                raise ContractError("ledger busy; refusing printer send") from None
            raise
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def begin_customer_send(self, row: ScheduleRow) -> str:
        """Compatibility projection; DispatchJob uses the fenced claim token."""
        return self.claim_customer_send(row).action

    def mark_customer(self, key: str, state: str, error: object = "", *, claim_token: str):
        if state not in {"retryable", "sent_pending_writeback", "complete", "ambiguous"}: raise ValueError("invalid state")
        with self._connect() as conn:
            expected = ("sent_pending_writeback",) if state == "complete" else (
                ("sending", "sent_pending_writeback") if state == "sent_pending_writeback" else ("sending",))
            placeholders = ",".join("?" for _ in expected)
            changed = conn.execute(f"""UPDATE customer_dispatch SET state=?,last_error=?,updated_at=?
              WHERE dispatch_key=? AND claim_token=? AND state IN ({placeholders})""",
              (state, str(error)[:200], self._now(), key, claim_token, *expected))
            if changed.rowcount != 1:
                raise ContractError("stale customer claim completion refused")

    def unsummarized_for_date(self, service_date: str):
        with self._connect() as conn:
            return conn.execute("""SELECT * FROM customer_dispatch WHERE service_date=?
              AND state IN ('sent_pending_writeback','complete')
              AND dispatch_key NOT IN (SELECT dispatch_key FROM summary_members) ORDER BY dispatch_key""", (service_date,)).fetchall()

    def claim_summary_send(self, key: str, service_date: str, members: Sequence[str]) -> "Ledger.Claim":
        token = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM summary_dispatch WHERE summary_key=?", (key,)).fetchone()
            if row:
                state = str(row[0])
                if state == "retryable":
                    changed = conn.execute("""UPDATE summary_dispatch SET state='sending',claim_token=?,last_error='',updated_at=?
                      WHERE summary_key=? AND state='retryable'""", (token, self._now(), key))
                    if changed.rowcount != 1:
                        raise ContractError("ledger summary retry claim lost; refusing printer send")
                    conn.commit(); return self.Claim("send", token)
                conn.commit(); return self.Claim("complete" if state == "complete" else "manual")
            conn.execute("""INSERT INTO summary_dispatch(
              summary_key,service_date,state,member_keys_json,last_error,updated_at,claim_token)
              VALUES(?,?,'sending',?,'',?,?)""",
              (key, service_date, json.dumps(list(members)), self._now(), token))
            conn.commit(); return self.Claim("send", token)
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if self._busy(exc):
                raise ContractError("ledger busy; refusing summary send") from None
            raise
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def begin_summary_send(self, key: str, service_date: str, members: Sequence[str]) -> str:
        return self.claim_summary_send(key, service_date, members).action

    def mark_summary(self, key: str, state: str, error: object = "", *, claim_token: str):
        with self._connect() as conn:
            changed = conn.execute("""UPDATE summary_dispatch SET state=?,last_error=?,updated_at=?
              WHERE summary_key=? AND claim_token=? AND state='sending'""",
              (state, str(error)[:200], self._now(), key, claim_token))
            if changed.rowcount != 1:
                raise ContractError("stale summary claim completion refused")
            if state == "complete":
                members = json.loads(conn.execute("SELECT member_keys_json FROM summary_dispatch WHERE summary_key=?", (key,)).fetchone()[0])
                conn.executemany("INSERT OR IGNORE INTO summary_members VALUES(?,?,?)", [(m, key, self._now()) for m in members])


@dataclass
class JobResult:
    dry_run_candidates: int = 0
    customer_dispatched: int = 0
    customer_already_complete: int = 0
    blocked_manual: int = 0
    ambiguous: int = 0
    writeback_failed: int = 0
    writeback_recovered: int = 0
    summary_dispatched: int = 0
    summary_ambiguous: int = 0
    summary_key: str = ""
    report_pending: int = 0
    report_recovered: int = 0
    warnings: list[str] = field(default_factory=list)


class DispatchJob:
    def __init__(self, rows: Iterable[ScheduleRow], ledger: Ledger, transport: Any, *, writeback_value: str = DEFAULT_WRITEBACK, already_printed: int = 0):
        self.rows, self.ledger, self.transport = tuple(rows), ledger, transport
        self.writeback_value = normalize_text(writeback_value)
        self.already_printed = already_printed
        if self.writeback_value != "已送印":
            raise ContractError("本版本 writeback 僅允許語意精確的「已送印」")

    @staticmethod
    def _warning(ticket: RenderedTicket, label: str):
        if not ticket.unsupported_characters: return None
        return f"{label}: CP950 不支援字元將以 ? 送印：" + " ".join(repr(c) for c in ticket.unsupported_characters)

    def _writeback(self, row: ScheduleRow):
        row.worksheet.batch_update([{"range": f"N{row.row_number}", "values": [[self.writeback_value]]}])

    def run(self, target: date, *, allow_print: bool = False) -> JobResult:
        if any(_iso_date(row.service_date, "ledger row") != target.isoformat() for row in self.rows):
            raise ContractError("dispatch rows 與 target date 不一致")
        result = JobResult(dry_run_candidates=len(self.rows) if not allow_print else 0, customer_already_complete=self.already_printed)
        if not allow_print: return result
        for row in self.rows:
            claim = self.ledger.claim_customer_send(row)
            action = claim.action
            if action == "complete": result.customer_already_complete += 1; continue
            if action == "manual": result.blocked_manual += 1; continue
            if action == "writeback":
                try: self._writeback(row)
                except Exception as exc: self.ledger.mark_customer(row.dispatch_key, "sent_pending_writeback", exc, claim_token=claim.token); result.writeback_failed += 1
                else: self.ledger.mark_customer(row.dispatch_key, "complete", claim_token=claim.token); result.writeback_recovered += 1
                continue
            ticket = render_customer_ticket(row.customer_name, row.service_date, row.lunch, row.dinner)
            warning = self._warning(ticket, f"{row.worksheet_title} 第{row.row_number}列")
            if warning: result.warnings.append(warning)
            try: self.transport.send(ticket.payload)
            except PreSendError as exc: self.ledger.mark_customer(row.dispatch_key, "retryable", exc, claim_token=claim.token); continue
            except (AmbiguousSendError, OSError) as exc: self.ledger.mark_customer(row.dispatch_key, "ambiguous", exc, claim_token=claim.token); result.ambiguous += 1; continue
            self.ledger.mark_customer(row.dispatch_key, "sent_pending_writeback", claim_token=claim.token); result.customer_dispatched += 1
            try: self._writeback(row)
            except Exception as exc: self.ledger.mark_customer(row.dispatch_key, "sent_pending_writeback", exc, claim_token=claim.token); result.writeback_failed += 1
            else: self.ledger.mark_customer(row.dispatch_key, "complete", claim_token=claim.token)
        service = target.strftime("%Y/%m/%d")
        dispatched = self.ledger.unsummarized_for_date(service)
        if dispatched:
            members = [str(r["dispatch_key"]) for r in dispatched]
            key = hashlib.sha256((service + "\x1f" + "\x1f".join(members) + "\x1f" + APP_VERSION).encode()).hexdigest()
            result.summary_key = key
            lunch: dict[str, int] = {}; dinner: dict[str, int] = {}
            for row in dispatched:
                for value, totals in ((str(row["lunch"]), lunch), (str(row["dinner"]), dinner)):
                    if not is_absent_meal(value): totals[value] = totals.get(value, 0) + 1
            if lunch or dinner:
                summary_claim = self.ledger.claim_summary_send(key, service, members)
                if summary_claim.action == "send":
                    ticket = render_summary(service, lunch, dinner)
                    warning = self._warning(ticket, "廚房摘要")
                    if warning: result.warnings.append(warning)
                    try: self.transport.send(ticket.payload)
                    except PreSendError as exc: self.ledger.mark_summary(key, "retryable", exc, claim_token=summary_claim.token)
                    except (AmbiguousSendError, OSError) as exc: self.ledger.mark_summary(key, "ambiguous", exc, claim_token=summary_claim.token); result.summary_ambiguous += 1
                    else: self.ledger.mark_summary(key, "complete", claim_token=summary_claim.token); result.summary_dispatched += 1
        return result


class V2DispatchJob:
    """Backend-authorized consumer: one permit, one packet, durable result-only retry."""
    def __init__(self, rows: Iterable[ScheduleRow], ledger: Ledger, transport: Any,
                 backend: BackendClaimClient):
        self.rows, self.ledger, self.transport, self.backend = tuple(rows), ledger, transport, backend

    @staticmethod
    def operation_id(row: ScheduleRow) -> str:
        # Stable across cache loss: backend replay still prevents a second packet.
        return "android-v2-" + hashlib.sha256(
            (row.dispatch_key + "\x1fbackend-claim-v2").encode("utf-8")
        ).hexdigest()

    def _report_pending(self, result: JobResult) -> None:
        for record in self.ledger.pending_v2_reports():
            try:
                self.backend.report(
                    operation_id=str(record["operation_id"]),
                    send_permit=str(record["send_permit"]), outcome=str(record["outcome"]),
                    ack_id=str(record["ack_id"]), evidence_digest=str(record["evidence_digest"]),
                )
            except ContractError:
                result.report_pending += 1
            else:
                self.ledger.complete_v2_report(str(record["operation_id"]))
                result.report_recovered += 1

    def run(self, target: date, *, allow_print: bool = False) -> JobResult:
        if any(_iso_date(row.service_date, "v2 row") != target.isoformat() for row in self.rows):
            raise ContractError("dispatch rows 與 target date 不一致")
        result = JobResult(dry_run_candidates=len(self.rows) if not allow_print else 0)
        if not allow_print:
            return result
        self._report_pending(result)
        for row in self.rows:
            operation_id = self.operation_id(row)
            existing = self.ledger.v2_record(operation_id)
            if existing:
                # complete/pending/sending are all permanent no-resend states. Pending
                # reports were handled above; sending requires manual reconciliation.
                state = str(existing["state"])
                if state == "complete":
                    result.customer_already_complete += 1
                    continue
                if state == "sending":
                    result.blocked_manual += 1
                    continue
                if state == "pending_report":
                    continue
                if state != "permitted":
                    raise ContractError("unknown local v2 state; refusing send")
                # Crash before the local send CAS is safe to resume: the durable
                # permit exists and no process had entered the sending state.
                permit = str(existing["send_permit"])
            else:
                permit = self.backend.claim(operation_id=operation_id, row=row)
                self.ledger.store_v2_permit(operation_id, row.dispatch_key, permit)
            self.ledger.claim_v2_send(operation_id)
            ticket = render_customer_ticket(row.customer_name, row.service_date, row.lunch, row.dinner)
            try:
                sent = self.transport.send(ticket.payload)
            except Exception:
                outcome, ack_id = "outcome_unknown", ""
                result.ambiguous += 1
            else:
                outcome = "transport_accepted"
                ack_id = hashlib.sha256(
                    (operation_id + ":" + str(sent) + ":" + hashlib.sha256(ticket.payload).hexdigest()).encode()
                ).hexdigest()
                result.customer_dispatched += 1
            evidence = hashlib.sha256(
                (operation_id + ":" + outcome + ":" + hashlib.sha256(ticket.payload).hexdigest()).encode()
            ).hexdigest()
            self.ledger.mark_v2_report(operation_id, outcome, ack_id, evidence)
            try:
                self.backend.report(operation_id=operation_id, send_permit=permit,
                                    outcome=outcome, ack_id=ack_id, evidence_digest=evidence)
            except ContractError:
                result.report_pending += 1
            else:
                self.ledger.complete_v2_report(operation_id)
        return result


def find_key_path(explicit: str | None = None) -> str:
    for candidate in ((explicit,) if explicit else KEY_CANDIDATES):
        if candidate and os.path.isfile(candidate): return candidate
    raise ContractError("找不到 Android 本機 Google service-account key 檔")


def connect_workbook(key_path: str, workbook_id: str) -> Any:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise ContractError("缺少 gspread/google-auth") from exc
    credentials = Credentials.from_service_account_file(key_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(credentials).open_by_key(workbook_id)


def build_parser():
    parser = argparse.ArgumentParser(description="南京店 Android backend-bound 出單（預設 dry-run）")
    parser.add_argument("--print", action="store_true", dest="allow_print")
    parser.add_argument("--date", help="YYYY/MM/DD；預設 Asia/Taipei 今天")
    parser.add_argument("--config", default="nanjing_printer_private.json")
    parser.add_argument("--key")
    parser.add_argument("--ledger", default="nanjing_dispatch_ledger.sqlite3")
    parser.add_argument("--printer-ip", default=DEFAULT_PRINTER_IP)
    parser.add_argument("--printer-port", type=int, default=DEFAULT_PRINTER_PORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = datetime.now(TAIPEI).date() if not args.date else datetime.strptime(args.date, "%Y/%m/%d").date()
        config = load_device_config(args.config)
        raw = fetch_dispatch_contract(config, target)
        records = parse_contract(raw, config, target)
        if config.dispatch_mode == "backend_v2":
            reconciled = Reconciliation(contract_rows_to_schedule(records), 0)
        else:
            workbook = connect_workbook(find_key_path(args.key), config.workbook_id)
            reconciled = reconcile_contract(workbook, records, config, target)
        mode = "PRINT" if args.allow_print else "DRY-RUN"
        print(f"模式={mode} 日期={target:%Y/%m/%d} store={config.store_id} 候選={len(reconciled.rows)} 已送印略過={reconciled.already_printed}")
        if config.dispatch_mode == "backend_v2":
            result = V2DispatchJob(
                reconciled.rows, Ledger(args.ledger),
                SocketTransport(args.printer_ip, args.printer_port), BackendClaimClient(config),
            ).run(target, allow_print=args.allow_print)
        else:
            result = DispatchJob(reconciled.rows, Ledger(args.ledger), SocketTransport(args.printer_ip, args.printer_port), already_printed=reconciled.already_printed).run(target, allow_print=args.allow_print)
        for warning in result.warnings: print("警告：" + warning, file=sys.stderr)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return 2 if (result.ambiguous or result.blocked_manual or result.summary_ambiguous or result.writeback_failed or result.report_pending) else 0
    except (ContractError, ValueError) as exc:
        print(f"安全停止：{exc}", file=sys.stderr); return 2
    except Exception as exc:
        # 不輸出 token、Google key、完整 UID/export payload 或底層 HTTP body。
        print(f"執行失敗：{type(exc).__name__}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
