from __future__ import annotations

import argparse
import csv
import io
import base64
import hashlib
import atexit
import ctypes
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import webbrowser
from datetime import datetime, timezone, timedelta, date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cloud_sync import CloudSyncManager
from zoneinfo import ZoneInfo

APP_NAME = "Portfolio Control"
APP_VERSION = "3.7.1"


def bundle_root() -> Path:
    # PyInstaller onedir/onefile exposes bundled data under sys._MEIPASS.
    # In source/dev mode we keep using the script directory.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


ROOT = bundle_root()
USER_AGENT = f"Portfolio-Control-v{APP_VERSION}/1.0"


def user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PortfolioControl"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PortfolioControl"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "PortfolioControl"


DATA_DIR = user_data_dir()
DB_PATH = DATA_DIR / "portfolio.db"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"
SECRETS_PATH = DATA_DIR / "secrets.json"
_NEWS_CACHE: dict[str, Any] = {"key": None, "time": 0.0, "payload": None}
_CALENDAR_CACHE: dict[str, Any] = {"time": 0.0, "payload": None}
_MACRO_CACHE: dict[str, Any] = {"time": 0.0, "payload": None}
_TRANSLATION_CACHE: dict[str, str] = {}
DB_LOCK = threading.RLock()
_INSTANCE_MUTEX_HANDLE: int | None = None
_INSTANCE_PID_PATH = DATA_DIR / "instance.pid"


def _close_server(server: ThreadingHTTPServer) -> None:
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def release_single_instance() -> None:
    """Release the Windows named mutex and remove our PID marker."""
    global _INSTANCE_MUTEX_HANDLE
    if sys.platform.startswith("win") and _INSTANCE_MUTEX_HANDLE:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = ctypes.c_void_p(_INSTANCE_MUTEX_HANDLE)
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        except Exception:
            pass
        _INSTANCE_MUTEX_HANDLE = None
    try:
        if _INSTANCE_PID_PATH.exists():
            raw = _INSTANCE_PID_PATH.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                _INSTANCE_PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _write_instance_pid() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _INSTANCE_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _read_instance_pid() -> int | None:
    try:
        pid = int(_INSTANCE_PID_PATH.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except Exception:
        return None


def _terminate_process_windows(pid: int) -> bool:
    """Terminate a stuck prior Portfolio Control process after user consent."""
    if not sys.platform.startswith("win") or pid <= 0 or pid == os.getpid():
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            if not kernel32.TerminateProcess(handle, 0):
                return False
            kernel32.WaitForSingleObject(handle, 3000)
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


atexit.register(release_single_instance)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','1')"
        )
        conn.commit()


def load_state_from_db() -> dict[str, Any] | None:
    ensure_storage()
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT state_json FROM app_state WHERE id=1").fetchone()
    if not row:
        return None
    try:
        state = json.loads(row[0])
        return state if isinstance(state, dict) else None
    except json.JSONDecodeError:
        return None


def save_state_to_db(state: dict[str, Any], mark_dirty: bool = True) -> None:
    ensure_storage()
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    updated = now_iso()
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO app_state(id,state_json,updated_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (raw, updated),
        )
        conn.commit()
    try:
        write_daily_backup(state)
    except Exception as exc:
        # The primary SQLite save succeeded.  A backup failure should be
        # visible in diagnostics but must never make the app unusable.
        print(f"WARNING: automatic backup failed: {exc}")
    if mark_dirty:
        try:
            schedule_cloud_sync()
        except NameError:
            # During module initialization CLOUD_SYNC/scheduler is not defined yet.
            pass
        except Exception as exc:
            print(f"WARNING: could not schedule cloud sync: {exc}")


CLOUD_SYNC = CloudSyncManager(DATA_DIR, DB_PATH, SECRETS_PATH, APP_VERSION)
_CLOUD_SYNC_TIMER: threading.Timer | None = None
_CLOUD_SYNC_TIMER_LOCK = threading.RLock()


def _run_scheduled_cloud_sync() -> None:
    global _CLOUD_SYNC_TIMER
    with _CLOUD_SYNC_TIMER_LOCK:
        _CLOUD_SYNC_TIMER = None
    if not CLOUD_SYNC.configured():
        return
    try:
        CLOUD_SYNC.sync(load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False))
    except Exception as exc:
        CLOUD_SYNC.record_error(str(exc))
        print(f"WARNING: background cloud sync failed: {exc}")


def schedule_cloud_sync(delay: float = 2.0) -> None:
    """Debounce cloud sync after local writes. Local SQLite always wins availability."""
    global _CLOUD_SYNC_TIMER
    if not CLOUD_SYNC.configured():
        return
    with _CLOUD_SYNC_TIMER_LOCK:
        try:
            if _CLOUD_SYNC_TIMER is not None:
                _CLOUD_SYNC_TIMER.cancel()
        except Exception:
            pass
        _CLOUD_SYNC_TIMER = threading.Timer(delay, _run_scheduled_cloud_sync)
        _CLOUD_SYNC_TIMER.daemon = True
        _CLOUD_SYNC_TIMER.start()


def _unique_tmp_path(final_path: Path) -> Path:
    """Return a temp path unique to this process/thread/write attempt."""
    token = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:10]}"
    return final_path.with_name(f".{final_path.name}.{token}.tmp")


def _replace_with_retry(src: Path, dst: Path, retries: int = 8) -> bool:
    """Atomically replace dst on Windows, tolerating short-lived file locks.

    Antivirus/indexers and an accidentally duplicated app instance can hold a
    backup file for a fraction of a second.  A backup failure must never take
    down the portfolio application, so we retry and let the caller fall back
    to a uniquely named recovery backup if the destination stays locked.
    """
    last_error: OSError | None = None
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return True
        except OSError as exc:
            last_error = exc
            winerror = getattr(exc, "winerror", None)
            if isinstance(exc, PermissionError) or winerror in (5, 32, 33):
                time.sleep(min(0.12 * (attempt + 1), 0.8))
                continue
            raise
    print(f"WARNING: backup destination is locked: {dst} ({last_error})")
    return False


def _cleanup_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # A security scanner may still be looking at the temp file.  It is
        # harmless; unique temp names prevent it from blocking future saves.
        pass


def write_daily_backup(state: dict[str, Any]) -> None:
    """Maintain daily JSON/SQLite backups without ever blocking app startup."""
    ensure_storage()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    json_path = BACKUP_DIR / f"portfolio_{day}.json"
    sqlite_path = BACKUP_DIR / f"portfolio_{day}.db"

    payload = {
        "schema": "portfolio-control-auto-backup",
        "schema_version": "1.0",
        "app_version": APP_VERSION,
        "generated_at": now_iso(),
        "app_state": state,
    }

    tmp_json = _unique_tmp_path(json_path)
    tmp_db = _unique_tmp_path(sqlite_path)
    try:
        tmp_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not _replace_with_retry(tmp_json, json_path):
            fallback = BACKUP_DIR / f"portfolio_recovery_{day}_{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}.json"
            try:
                os.replace(tmp_json, fallback)
                print(f"Backup fallback written: {fallback}")
            except OSError as exc:
                print(f"WARNING: JSON auto-backup skipped: {exc}")

        # sqlite3.Connection.backup() is the SQLite-supported way to copy a
        # live database safely.  The destination temp name is unique so a stale
        # temp file from an earlier run cannot collide with this write.
        with DB_LOCK, sqlite3.connect(DB_PATH) as src, sqlite3.connect(tmp_db) as dst:
            src.backup(dst)
            dst.commit()

        if not _replace_with_retry(tmp_db, sqlite_path):
            fallback = BACKUP_DIR / f"portfolio_recovery_{day}_{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}.db"
            try:
                os.replace(tmp_db, fallback)
                print(f"Backup fallback written: {fallback}")
            except OSError as exc:
                print(f"WARNING: SQLite auto-backup skipped: {exc}")
        prune_backups(days=60)
    finally:
        _cleanup_temp(tmp_json)
        _cleanup_temp(tmp_db)


def prune_backups(days: int = 60) -> None:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    for p in BACKUP_DIR.glob("portfolio_20??-??-??.*"):
        try:
            stamp = p.stem.replace("portfolio_", "")
            d = datetime.strptime(stamp, "%Y-%m-%d").astimezone()
            if d < cutoff:
                p.unlink(missing_ok=True)
        except Exception:
            continue


def force_backup() -> dict[str, str]:
    state = load_state_from_db()
    if state is None:
        raise ValueError("数据库中还没有持仓数据")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")
    json_path = BACKUP_DIR / f"portfolio_manual_{stamp}.json"
    db_path = BACKUP_DIR / f"portfolio_manual_{stamp}.db"
    payload = {
        "schema": "portfolio-control-manual-backup",
        "schema_version": "1.0",
        "app_version": APP_VERSION,
        "generated_at": now_iso(),
        "app_state": state,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with DB_LOCK, sqlite3.connect(DB_PATH) as src, sqlite3.connect(db_path) as dst:
        src.backup(dst)
    return {"json": str(json_path), "database": str(db_path)}


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def normalize_longbridge_symbol(ticker: str) -> str:
    s = (ticker or "").strip().upper().replace(" ", "")
    if not s:
        raise ValueError("股票代码为空")
    if "." not in s:
        s = f"{int(s)}.HK" if s.isdigit() else f"{s}.US"
    code, market = s.rsplit(".", 1)
    if market == "HK" and code.isdigit():
        code = str(int(code))
    if market not in {"US", "HK", "SH", "SZ", "SG"}:
        raise ValueError(f"暂不识别市场后缀 .{market}")
    return f"{code}.{market}"


def stock_currency(symbol: str) -> str:
    market = symbol.rsplit(".", 1)[-1]
    return {"US": "USD", "HK": "HKD", "SH": "CNY", "SZ": "CNY", "SG": "SGD"}.get(market, "USD")


def normalize_binance_symbol(ticker: str) -> str:
    s = (ticker or "").upper().strip().replace("/", "").replace("-", "").replace("_", "")
    if not s:
        raise ValueError("加密货币代码为空")
    # USDT is the quote currency for the rest of the crypto portfolio.  A bare
    # USDT holding must therefore be valued at 1 USDT, not queried as the
    # nonexistent USDTUSDT pair.  Keep the canonical marker as USDT and let
    # fetch_binance() return the peg value locally.
    if s in {"USDT", "USDTUSDT"}:
        return "USDT"
    if s.endswith("USDT") and len(s) > 4 and s[:-4].isalnum():
        return s
    if s.endswith("USD") and len(s) > 3:
        s = s[:-3]
    if s.isalnum():
        return s + "USDT"
    raise ValueError("无法识别 Binance 代码")


def choose_longbridge_price(row: dict[str, Any], now: datetime | None = None) -> tuple[float, str, str | None]:
    symbol = str(row.get("symbol", "")).upper()
    market = symbol.rsplit(".", 1)[-1] if "." in symbol else ""

    def nested_price(key: str) -> tuple[float, str | None] | None:
        block = row.get(key)
        if not isinstance(block, dict):
            return None
        try:
            price = float(block.get("last"))
        except (TypeError, ValueError):
            return None
        return price, block.get("timestamp")

    try:
        regular = float(row.get("last"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Longbridge quote has no valid last price") from exc

    if market != "US":
        return regular, "常规盘", None

    ny_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    day = ny_now.weekday()
    minute = ny_now.hour * 60 + ny_now.minute

    if day <= 4 and 4 * 60 <= minute < 9 * 60 + 30:
        q = nested_price("pre_market")
        if q:
            return q[0], "盘前", q[1]
    if day <= 4 and 9 * 60 + 30 <= minute < 16 * 60:
        return regular, "常规盘", None
    if day <= 4 and 16 * 60 <= minute < 20 * 60:
        q = nested_price("post_market")
        if q:
            return q[0], "盘后", q[1]
    if (day <= 4 and minute < 4 * 60) or (day <= 3 and minute >= 20 * 60) or (day == 6 and minute >= 20 * 60):
        q = nested_price("overnight")
        if q:
            return q[0], "隔夜", q[1]
    return regular, "常规盘", None


def find_longbridge_executable() -> str | None:
    # Windows installer bundles the official CLI. macOS builds may also bundle
    # an architecture-matching binary when it is present on the build Mac.
    bundled_names = ["longbridge.exe", "longbridge"] if sys.platform.startswith("win") else ["longbridge", "longbridge.exe"]
    for name in bundled_names:
        bundled = ROOT / "tools" / name
        if bundled.exists():
            return str(bundled)
    exe = shutil.which("longbridge")
    if exe:
        return exe
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "longbridge" / "longbridge.exe")
    elif sys.platform == "darwin":
        candidates += [Path("/opt/homebrew/bin/longbridge"), Path("/usr/local/bin/longbridge"), Path.home()/".local"/"bin"/"longbridge"]
    for fallback in candidates:
        if fallback.exists():
            return str(fallback)
    return None


def longbridge_auth_ready(exe: str) -> bool:
    try:
        cp = subprocess.run(
            [exe, "auth", "status"],
            capture_output=True, text=True, timeout=12, check=False,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0),
        )
        return cp.returncode == 0
    except Exception:
        return False


def ensure_longbridge_authorized_first_run() -> None:
    exe = find_longbridge_executable()
    if not exe or longbridge_auth_ready(exe):
        return
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                "Portfolio Control 需要一次性连接你的长桥账户以读取股票行情。\n\n点击“确定”后浏览器会打开长桥 OAuth 授权页面。授权完成后应用会继续启动。",
                f"{APP_NAME} · Longbridge",
                0x40,
            )
        except Exception:
            pass
    try:
        cp = subprocess.run(
            [exe, "auth", "login"],
            capture_output=True, text=True, timeout=300, check=False,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0),
        )
        if cp.returncode != 0 and sys.platform.startswith("win"):
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "长桥授权没有完成。Portfolio Control 仍会打开，但股票自动行情暂时不可用。\n你可以稍后重新启动应用再次授权。",
                    APP_NAME,
                    0x30,
                )
            except Exception:
                pass
    except Exception:
        # Do not block access to the rest of the portfolio app if authorization fails.
        pass


def fetch_longbridge(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], str | None]:
    exe = find_longbridge_executable()
    if not exe:
        return {}, "未找到 Longbridge CLI。请重新安装 Portfolio Control 或安装 Longbridge CLI。"
    if not symbols:
        return {}, None
    try:
        cp = subprocess.run([exe, "quote", *symbols, "--format", "json"], cwd=str(ROOT), capture_output=True, text=True, timeout=25, check=False)
    except subprocess.TimeoutExpired:
        return {}, "Longbridge 行情请求超时。"
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "Longbridge CLI 返回错误").strip()
        return {}, msg[-800:]
    try:
        rows = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {}, "Longbridge CLI 未返回可解析的 JSON；请先测试 longbridge quote NVDA.US --format json。"
    out: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        sym = str(row.get("symbol", "")).upper()
        try:
            price, session, quote_time = choose_longbridge_price(row)
            regular_price = float(row.get("last"))
        except (TypeError, ValueError):
            continue
        out[sym] = {"price": price, "regular_price": regular_price, "session": session, "quote_time": quote_time, "prev_close": row.get("prev_close"), "status": row.get("status")}
    return out, None


def fetch_binance(symbol: str) -> tuple[float | None, str | None]:
    # USDT itself is the portfolio quote unit.  Returning 1 locally avoids an
    # invalid USDTUSDT request while the HKD value still follows USD/USDT FX.
    if symbol == "USDT":
        return 1.0, None
    base = "https://data-api.binance.vision/api/v3/ticker/price"
    url = base + "?" + urllib.parse.urlencode({"symbol": symbol})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return float(data["price"]), None
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, json.JSONDecodeError) as e:
        return None, f"Binance 行情失败：{e}"


def _read_json_url(url: str, timeout: int = 10) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))




class _TableRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "tr":
            self._row = []
        elif t in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif t == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _read_text_url(url: str, timeout: int = 12, accept: str = "text/html,*/*") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _date_from_any(value: str) -> date | None:
    text = (value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        text = str(value).strip().replace("%", "")
        if not text or text.upper() in {"N/A", "NA", ".", "NONE"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _fetch_treasury_html_series(days: int = 75) -> tuple[list[dict[str, Any]], str | None]:
    """Read 2Y/10Y par yields from the U.S. Treasury Daily Treasury Rates page."""
    today = datetime.now().astimezone().date()
    start = today - timedelta(days=max(days, 35) + 10)
    all_rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for yr in sorted({start.year, today.year}):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?"
               + urllib.parse.urlencode({"type": "daily_treasury_yield_curve", "field_tdr_date_value": str(yr)}))
        try:
            html = _read_text_url(url, 15)
            parser = _TableRowsParser(); parser.feed(html)
            idx2 = idx10 = None
            for row in parser.rows:
                norm = [re.sub(r"\\s+", " ", x.strip()).upper() for x in row]
                if "DATE" in norm and any(x in {"2 YR", "2 YEAR", "2-YR"} for x in norm) and any(x in {"10 YR", "10 YEAR", "10-YR"} for x in norm):
                    idx2 = next(i for i,x in enumerate(norm) if x in {"2 YR", "2 YEAR", "2-YR"})
                    idx10 = next(i for i,x in enumerate(norm) if x in {"10 YR", "10 YEAR", "10-YR"})
                    continue
                if idx2 is None or idx10 is None or len(row) <= max(idx2, idx10):
                    continue
                d = _date_from_any(row[0])
                if not d or d < start or d > today:
                    continue
                y2, y10 = _float_or_none(row[idx2]), _float_or_none(row[idx10])
                if y2 is None and y10 is None:
                    continue
                all_rows[d.isoformat()] = {"date": d.isoformat(), "us2y": y2, "us10y": y10,
                                           "spread_bp": ((y10-y2)*100 if y2 is not None and y10 is not None else None)}
        except Exception as exc:
            errors.append(f"{yr}: {exc}")
    rows = sorted(all_rows.values(), key=lambda x: x["date"])
    return rows, None if rows else ("Treasury 页面解析失败" + ("：" + "; ".join(errors[-2:]) if errors else ""))


def _fetch_fred_treasury_series(days: int = 75) -> tuple[list[dict[str, Any]], str | None]:
    """No-key fallback from the Federal Reserve Bank of St. Louis (DGS2/DGS10)."""
    start = (datetime.now().astimezone().date() - timedelta(days=max(days, 35)+10)).isoformat()
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode({"id": "DGS2,DGS10", "cosd": start})
    try:
        text = _read_text_url(url, 15, "text/csv,*/*")
        reader = csv.DictReader(io.StringIO(text))
        rows=[]
        for r in reader:
            ds = r.get("DATE") or r.get("observation_date") or ""
            d = _date_from_any(ds)
            if not d: continue
            y2 = _float_or_none(r.get("DGS2")); y10 = _float_or_none(r.get("DGS10"))
            if y2 is None and y10 is None: continue
            rows.append({"date":d.isoformat(),"us2y":y2,"us10y":y10,"spread_bp":((y10-y2)*100 if y2 is not None and y10 is not None else None)})
        return sorted(rows,key=lambda x:x["date"]), None if rows else "FRED 未返回 DGS2/DGS10 数据"
    except Exception as exc:
        return [], f"FRED 国债收益率失败：{exc}"


def _extract_nyfed_ref_rates(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict):
        if isinstance(obj.get("refRates"), list):
            return obj["refRates"]
        for v in obj.values():
            got = _extract_nyfed_ref_rates(v)
            if got: return got
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in ("percentRate","effectiveDate","type")):
            return obj
        for v in obj:
            got = _extract_nyfed_ref_rates(v)
            if got: return got
    return []


def _fetch_effr_series(number: int = 45) -> tuple[list[dict[str, Any]], str | None]:
    url = f"https://markets.newyorkfed.org/api/rates/unsecured/effr/last/{max(5,min(number,120))}.json"
    try:
        data = _read_json_url(url, 15)
        rows=[]
        for r in _extract_nyfed_ref_rates(data):
            typ = str(r.get("type") or r.get("rateType") or "EFFR").upper()
            if typ and "EFFR" not in typ: continue
            ds = str(r.get("effectiveDate") or r.get("date") or r.get("businessDate") or "")[:10]
            d = _date_from_any(ds)
            rate = _float_or_none(r.get("percentRate") if r.get("percentRate") is not None else r.get("rate"))
            if d and rate is not None:
                rows.append({"date":d.isoformat(),"effr":rate,"volume_billions":_float_or_none(r.get("volumeInBillions") or r.get("volume")),"target":r.get("targetRateRange") or r.get("targetRate")})
        dedup={x["date"]:x for x in rows}
        out=sorted(dedup.values(),key=lambda x:x["date"])
        return out, None if out else "NY Fed 未返回可解析的 EFFR 数据"
    except Exception as exc:
        return [], f"NY Fed EFFR 失败：{exc}"


def _series_bp_change(rows: list[dict[str, Any]], key: str, days: int) -> float | None:
    valid=[r for r in rows if _float_or_none(r.get(key)) is not None and _date_from_any(str(r.get("date") or ""))]
    if len(valid)<2: return None
    latest=valid[-1]; ld=_date_from_any(latest["date"])
    if not ld: return None
    target=ld-timedelta(days=days)
    prior=None
    for r in valid:
        rd=_date_from_any(r["date"])
        if rd and rd<=target: prior=r
    if prior is None: prior=valid[0]
    return (float(latest[key])-float(prior[key]))*100


def build_macro_indicators(force: bool = False) -> dict[str, Any]:
    now=time.time()
    if not force and _MACRO_CACHE.get("payload") and now-float(_MACRO_CACHE.get("time") or 0)<1800:
        out=dict(_MACRO_CACHE["payload"]); out["cached"]=True; return out
    errors=[]
    treasury, terr=_fetch_treasury_html_series(75)
    tsy_source="U.S. Treasury"
    if not treasury:
        treasury, ferr=_fetch_fred_treasury_series(75); tsy_source="FRED · U.S. Treasury source"
        if terr: errors.append(terr)
        if ferr: errors.append(ferr)
    elif terr: errors.append(terr)
    effr, eerr=_fetch_effr_series(45)
    if eerr: errors.append(eerr)
    latest_t=treasury[-1] if treasury else {}
    latest_e=effr[-1] if effr else {}
    latest={"us2y":latest_t.get("us2y"),"us10y":latest_t.get("us10y"),"spread2s10s_bp":latest_t.get("spread_bp"),"treasury_date":latest_t.get("date"),"effr":latest_e.get("effr"),"effr_date":latest_e.get("date"),"effr_target":latest_e.get("target")}
    changes={
      "us2y_1d_bp":_series_bp_change(treasury,"us2y",1),"us2y_7d_bp":_series_bp_change(treasury,"us2y",7),"us2y_30d_bp":_series_bp_change(treasury,"us2y",30),
      "us10y_1d_bp":_series_bp_change(treasury,"us10y",1),"us10y_7d_bp":_series_bp_change(treasury,"us10y",7),"us10y_30d_bp":_series_bp_change(treasury,"us10y",30),
      "spread_7d_bp":_series_bp_change(treasury,"spread_bp",7),"spread_30d_bp":_series_bp_change(treasury,"spread_bp",30),
      "effr_7d_bp":_series_bp_change(effr,"effr",7),"effr_30d_bp":_series_bp_change(effr,"effr",30),
    }
    # spread_bp is already basis points; the generic helper multiplies by 100, correct it.
    for k in ("spread_7d_bp","spread_30d_bp"):
        if changes[k] is not None: changes[k] /= 100
    alerts=[]
    d1=changes.get("us2y_1d_bp")
    if d1 is not None and abs(d1)>=10:
        alerts.append({"level":"high","title":"美国 2Y 单日大幅波动","message":f"2年期收益率单日变化 {d1:+.0f} bp；短端利率预期变化较快。"})
    d7=changes.get("us2y_7d_bp")
    if d7 is not None and abs(d7)>=20:
        alerts.append({"level":"medium","title":"美国 2Y 一周明显变动","message":f"过去约一周累计变化 {d7:+.0f} bp。"})
    sp=latest.get("spread2s10s_bp")
    if sp is not None and float(sp)<0:
        alerts.append({"level":"medium","title":"2s10s 收益率曲线倒挂","message":f"10Y-2Y 利差 {float(sp):+.0f} bp。"})
    payload={"ok":bool(treasury or effr),"asof":now_iso(),"cached":False,"latest":latest,"changes":changes,"treasury_series":treasury[-60:],"effr_series":effr[-45:],"sources":[{"name":tsy_source,"ok":bool(treasury),"url":"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"},{"name":"New York Fed · EFFR","ok":bool(effr),"url":"https://markets.newyorkfed.org/api/rates/unsecured/effr/last/45.json"}],"alerts":alerts,"errors":errors}
    _MACRO_CACHE.update({"time":now,"payload":payload})
    return payload


def load_local_secrets() -> dict[str, Any]:
    try:
        obj = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def save_local_secrets(obj: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SECRETS_PATH)


def _read_bytes_url(url: str, timeout: int = 12, headers: dict[str, str] | None = None) -> bytes:
    hdr = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _iso_from_rss_date(value: str | None) -> str | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return value


def _parse_datetime_any(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_fed_monetary_news(limit: int = 12) -> tuple[list[dict[str, Any]], str | None]:
    feeds = [
        ("https://www.federalreserve.gov/feeds/press_monetary.xml", "货币政策", 0),
        ("https://www.federalreserve.gov/feeds/s_t_powell.xml", "Powell 讲话", 6),
    ]
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for url, category, bonus in feeds:
        try:
            raw = _read_bytes_url(url, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            root = ET.fromstring(raw)
            for node in root.findall(".//item")[:limit]:
                title = (node.findtext("title") or "").strip()
                link = (node.findtext("link") or "").strip()
                desc = (node.findtext("description") or "").strip()
                pub = _iso_from_rss_date(node.findtext("pubDate"))
                t = title.lower()
                score = (96 if any(k in t for k in ("fomc", "federal funds", "monetary policy", "interest rate")) else 84) + bonus
                score = min(99, score)
                items.append({"id": f"fed:{link or title}", "source": "Federal Reserve", "source_type": "official", "title": title, "summary": desc, "url": link, "published_at": pub, "importance": "high" if score >= 90 else "medium", "score": score, "related_assets": [], "impact_classes": ["stock", "gold", "crypto"], "category": category})
        except Exception as exc:
            errors.append(f"{category}: {exc}")
    items.sort(key=lambda x: (float(x.get("score") or 0), str(x.get("published_at") or "")), reverse=True)
    return items[:limit], ("; ".join(errors) if errors and not items else None)


def _find_hkma_rows(obj: Any) -> list[dict[str, Any]]:
    """Accept several HKMA response shapes and locate the press-release rows."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in ("title", "date", "link")):
            return obj
        for v in obj:
            rows = _find_hkma_rows(v)
            if rows:
                return rows
    elif isinstance(obj, dict):
        for key in ("records", "data", "datas", "items", "rows"):
            v = obj.get(key)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                if not v or any(k in v[0] for k in ("title", "date", "link")):
                    return v
        for v in obj.values():
            if isinstance(v, (dict, list)):
                rows = _find_hkma_rows(v)
                if rows:
                    return rows
    return []


def _score_hkma_title(title: str) -> float:
    low = title.lower()
    high_words = (
        "base rate", "exchange fund", "monetary", "interest rate", "hong kong dollar", "hkd", "currency", "liquidity",
        "基本利率", "外汇基金", "外匯基金", "货币", "貨幣", "利率", "港元", "流动性", "流動性", "美联储", "美聯儲"
    )
    return 90 if any(k in low for k in high_words) else 70


def _parse_hkma_rss(raw: bytes, limit: int) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items: list[dict[str, Any]] = []
    for node in root.findall('.//item')[:limit]:
        title = (node.findtext('title') or '').strip()
        link = (node.findtext('link') or '').strip()
        desc = (node.findtext('description') or '').strip()
        pub = _iso_from_rss_date(node.findtext('pubDate'))
        score = _score_hkma_title(title)
        items.append({"id": f"hkma:{link or title}", "source": "HKMA", "source_type": "official", "title": title, "summary": desc, "url": link, "published_at": pub, "importance": "high" if score >= 85 else "medium", "score": score, "related_assets": [], "impact_classes": ["stock", "gold", "cash"], "category": "香港金融"})
    return items


def fetch_hkma_press_news(limit: int = 12) -> tuple[list[dict[str, Any]], str | None]:
    """HKMA Open API first; fall back across languages and then the official RSS feed."""
    errors: list[str] = []
    for lang in ("sc", "tc", "en"):
        url = f"https://api.hkma.gov.hk/public/press-releases?lang={lang}&offset=0&pagesize={limit}"
        try:
            data = _read_json_url(url, timeout=12)
            rows = _find_hkma_rows(data)
            if not rows:
                raise ValueError("返回内容中未找到新闻记录")
            items: list[dict[str, Any]] = []
            for row in rows[:limit]:
                title = str(row.get("title") or "").strip()
                link = str(row.get("link") or "").strip()
                d = str(row.get("date") or "").strip()
                if not title:
                    continue
                score = _score_hkma_title(title)
                items.append({"id": f"hkma:{link or title}", "source": "HKMA", "source_type": "official", "title": title, "summary": "", "url": link, "published_at": (d + "T00:00:00+08:00") if d else None, "importance": "high" if score >= 85 else "medium", "score": score, "related_assets": [], "impact_classes": ["stock", "gold", "cash"], "category": "香港金融"})
            if items:
                return items, None
        except Exception as exc:
            errors.append(f"Open API {lang}: {exc}")
    rss_urls = [
        "https://www.hkma.gov.hk/eng/other-information/rss/rss_press-release.xml",
    ]
    for url in rss_urls:
        try:
            raw = _read_bytes_url(url, timeout=12, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            items = _parse_hkma_rss(raw, limit)
            if items:
                return items, None
        except Exception as exc:
            errors.append(f"RSS: {exc}")
    return [], "HKMA 官方源失败：" + "; ".join(errors[-4:])


def _marketaux_symbol(asset: dict[str, Any]) -> str | None:
    typ = str(asset.get("type") or "")
    ticker = str(asset.get("ticker") or "").strip().upper()
    if not ticker:
        return None
    if typ == "stock":
        ticker = ticker.replace(".US", "")
        if ticker.endswith(".HK"):
            ticker = ticker[:-3].lstrip("0") or "0"
        return ticker
    if typ == "crypto":
        for suf in ("USDT", "USD"):
            if ticker.endswith(suf) and len(ticker) > len(suf):
                ticker = ticker[:-len(suf)]
        return ticker
    return None


def fetch_marketaux_news(assets: list[dict[str, Any]], token: str, limit: int = 3) -> tuple[list[dict[str, Any]], str | None]:
    symbols = []
    symbol_assets: dict[str, list[dict[str, Any]]] = {}
    for a in assets:
        sym = _marketaux_symbol(a)
        if not sym:
            continue
        if sym not in symbol_assets:
            symbols.append(sym)
            symbol_assets[sym] = []
        symbol_assets[sym].append(a)
    if not symbols:
        return [], None
    params = {"api_token": token, "symbols": ",".join(symbols[:40]), "filter_entities": "true", "language": "en", "limit": str(max(1, min(limit, 3)))}
    url = "https://api.marketaux.com/v1/news/all?" + urllib.parse.urlencode(params)
    try:
        data = _read_json_url(url, timeout=15)
        rows = data.get("data") or []
        items: list[dict[str, Any]] = []
        for row in rows:
            entities = row.get("entities") or []
            related: list[str] = []
            related_names: list[str] = []
            sentiments: list[float] = []
            max_weight = 0.0
            for ent in entities:
                sym = str(ent.get("symbol") or "").upper()
                if sym in symbol_assets:
                    for a in symbol_assets[sym]:
                        aid = str(a.get("id") or "")
                        if aid and aid not in related:
                            related.append(aid)
                            related_names.append(str(a.get("name") or sym))
                            max_weight = max(max_weight, float(a.get("weight_pct") or 0))
                    try:
                        sentiments.append(float(ent.get("sentiment_score")))
                    except Exception:
                        pass
            score = min(99, 72 + min(18, max_weight * 1.2) + min(8, max(0, len(related)-1)*3))
            sent = (sum(sentiments)/len(sentiments)) if sentiments else None
            items.append({"id": "marketaux:" + str(row.get("uuid") or row.get("url") or row.get("title")), "source": str(row.get("source") or "Marketaux"), "source_type": "aggregator", "title": str(row.get("title") or ""), "summary": str(row.get("description") or row.get("snippet") or ""), "url": str(row.get("url") or ""), "published_at": row.get("published_at"), "importance": "high" if score >= 86 else "medium", "score": round(score, 1), "related_assets": related, "related_asset_names": related_names, "impact_classes": [], "category": "持仓相关新闻", "sentiment": sent})
        return items, None
    except Exception as exc:
        return [], f"Marketaux 失败：{exc}"


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _split_utf8_chunks(text: str, max_bytes: int = 440) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for piece in re.split(r"(?<=[.!?。！？;；])\s+|\n+", text):
        piece = piece.strip()
        if not piece:
            continue
        candidate = (cur + " " + piece).strip() if cur else piece
        if len(candidate.encode("utf-8")) <= max_bytes:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        while len(piece.encode("utf-8")) > max_bytes:
            cut = max(1, int(len(piece) * max_bytes / max(1, len(piece.encode("utf-8")))))
            while len(piece[:cut].encode("utf-8")) > max_bytes and cut > 1:
                cut -= 1
            chunks.append(piece[:cut])
            piece = piece[cut:]
        cur = piece
    if cur:
        chunks.append(cur)
    return chunks


def translate_en_to_zh(text: str) -> tuple[str | None, str | None]:
    text = (text or "").strip()
    if not text:
        return "", None
    if _contains_cjk(text):
        return text, None
    if text in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[text], None
    out: list[str] = []
    try:
        for chunk in _split_utf8_chunks(text):
            params = urllib.parse.urlencode({"q": chunk, "langpair": "en|zh-CN", "mt": "1"})
            data = _read_json_url("https://api.mymemory.translated.net/get?" + params, timeout=15)
            translated = str(((data or {}).get("responseData") or {}).get("translatedText") or "").strip()
            import html as _html
            translated = _html.unescape(translated)
            if not translated:
                raise ValueError("翻译服务未返回文本")
            out.append(translated)
        result = " ".join(out).strip()
        _TRANSLATION_CACHE[text] = result
        return result, None
    except Exception as exc:
        return None, f"翻译失败：{exc}"


def _html_to_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    import html as _html
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _event_in_horizon(event_date: date, start: date, end: date) -> bool:
    return start <= event_date <= end


def _et_to_hk_date_time(year: int, month: int, day: int, hhmm: str, ampm: str) -> tuple[str, str]:
    hour, minute = map(int, hhmm.split(":"))
    if ampm.upper() == "PM" and hour != 12:
        hour += 12
    if ampm.upper() == "AM" and hour == 12:
        hour = 0
    et = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    hk = et.astimezone(ZoneInfo("Asia/Hong_Kong"))
    return hk.date().isoformat(), hk.strftime("%H:%M")


def fetch_bls_calendar(start: date, end: date) -> tuple[list[dict[str, Any]], str | None]:
    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    try:
        raw = _read_bytes_url(url, timeout=15, headers={"Accept": "text/calendar,text/plain"}).decode("utf-8", errors="ignore")
        raw = re.sub(r"\r?\n[ \t]", "", raw)
        blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, flags=re.S)
        specs = [
            ("Consumer Price Index", "美国 CPI", "high", ["stock", "gold", "crypto"]),
            ("Employment Situation", "美国非农就业报告", "high", ["stock", "gold", "crypto"]),
            ("Producer Price Index", "美国 PPI", "high", ["stock", "gold", "crypto"]),
            ("Job Openings and Labor Turnover", "美国 JOLTS 职位空缺", "medium", ["stock", "gold"]),
            ("Employment Cost Index", "美国 ECI 就业成本指数", "medium", ["stock", "gold"]),
        ]
        items: list[dict[str, Any]] = []
        for block in blocks:
            sm = re.search(r"^SUMMARY(?:;[^:]*)?:(.+)$", block, flags=re.M)
            dm = re.search(r"^DTSTART(?:;TZID=([^:]+))?:(\d{8})(?:T(\d{6}))?", block, flags=re.M)
            if not sm or not dm:
                continue
            summary = sm.group(1).replace("\\,", ",").strip()
            spec = next((x for x in specs if x[0].lower() in summary.lower()), None)
            if not spec:
                continue
            d8, t6 = dm.group(2), dm.group(3) or "083000"
            y, mo, da = int(d8[:4]), int(d8[4:6]), int(d8[6:8])
            tzname = dm.group(1) or "America/New_York"
            try:
                tz = ZoneInfo(tzname)
            except Exception:
                tz = ZoneInfo("America/New_York")
            dt = datetime(y, mo, da, int(t6[:2]), int(t6[2:4]), int(t6[4:6]), tzinfo=tz).astimezone(ZoneInfo("Asia/Hong_Kong"))
            if not _event_in_horizon(dt.date(), start, end):
                continue
            _, zh, importance, impacts = spec
            items.append({"id": f"bls:{d8}:{spec[0]}", "source": "BLS", "source_type": "official", "title": summary, "title_zh": zh, "date": dt.date().isoformat(), "time_hkt": dt.strftime("%H:%M"), "end_date": None, "importance": importance, "category": "美国宏观", "impact_classes": impacts, "url": "https://www.bls.gov/schedule/", "note": "美国劳工统计局官方发布日程；时间已转换为香港时间。", "system": True})
        return items, None
    except Exception as exc:
        return [], f"BLS 日历失败：{exc}"


def fetch_fed_calendar(start: date, end: date) -> tuple[list[dict[str, Any]], str | None]:
    url = "https://www.federalreserve.gov/monetarypolicy.htm"
    try:
        text = _html_to_text(_read_bytes_url(url, timeout=15))
        months = {"Jan.":1,"Feb.":2,"Mar.":3,"Apr.":4,"May":5,"June":6,"July":7,"Aug.":8,"Sept.":9,"Sep.":9,"Oct.":10,"Nov.":11,"Dec.":12}
        pat = re.compile(r"\b(Jan\.|Feb\.|Mar\.|Apr\.|May|June|July|Aug\.|Sept\.|Sep\.|Oct\.|Nov\.|Dec\.)\s+(\d{1,2}(?:-\d{1,2})?)\s+(FOMC Meeting|FOMC Minutes)", re.I)
        items: list[dict[str, Any]] = []
        for m in pat.finditer(text):
            mon_token = m.group(1)
            mon = months.get(mon_token[0].upper()+mon_token[1:], months.get(mon_token, 0))
            if not mon:
                mon = next((v for k,v in months.items() if k.lower()==mon_token.lower()),0)
            days = m.group(2)
            first_day = int(days.split('-')[0]); last_day = int(days.split('-')[-1])
            year = start.year
            if mon < start.month - 6:
                year += 1
            dt = date(year, mon, first_day)
            if not _event_in_horizon(dt, start, end) and not (first_day != last_day and _event_in_horizon(date(year,mon,last_day), start, end)):
                continue
            kind = m.group(3)
            is_meeting = "Meeting" in kind
            title_zh = "FOMC 议息会议" if is_meeting else "FOMC 会议纪要发布"
            items.append({"id": f"fedcal:{year}-{mon:02d}-{first_day:02d}:{kind}", "source": "Federal Reserve", "source_type": "official", "title": f"{kind} ({m.group(1)} {days})", "title_zh": title_zh, "date": dt.isoformat(), "end_date": date(year,mon,last_day).isoformat() if last_day != first_day else None, "time_hkt": None, "importance": "high", "category": "美联储", "impact_classes": ["stock", "gold", "crypto"], "url": "https://www.federalreserve.gov/monetarypolicy.htm", "note": "美联储官方 Upcoming Dates；FOMC 会议对利率、股票、黄金及加密资产均属高影响事件。", "system": True})
        return items, None
    except Exception as exc:
        return [], f"Fed 日历失败：{exc}"


def fetch_bea_calendar(start: date, end: date) -> tuple[list[dict[str, Any]], str | None]:
    url = "https://www.bea.gov/news/schedule"
    try:
        text = _html_to_text(_read_bytes_url(url, timeout=15))
        month_names = "January|February|March|April|May|June|July|August|September|October|November|December"
        date_re = re.compile(rf"\b({month_names})\s+(\d{{1,2}})\s+(\d{{1,2}}:\d{{2}})\s+(AM|PM)\b", re.I)
        month_map = {m:i for i,m in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"],1)}
        targets = [("Personal Income and Outlays", "美国 PCE / 个人收入与支出", "high"), ("GDP (", "美国 GDP", "high")]
        items: list[dict[str, Any]] = []
        lower = text.lower()
        for needle, zh, importance in targets:
            pos = 0
            needle_lower = needle.lower()
            while True:
                idx = lower.find(needle_lower, pos)
                if idx < 0:
                    break
                prefix = text[max(0, idx-180):idx]
                matches = list(date_re.finditer(prefix))
                if matches:
                    dm = matches[-1]
                    mon = month_map[dm.group(1).capitalize()]; da = int(dm.group(2)); year = start.year
                    if mon < start.month - 6:
                        year += 1
                    hk_date, hk_time = _et_to_hk_date_time(year, mon, da, dm.group(3), dm.group(4))
                    d = date.fromisoformat(hk_date)
                    if _event_in_horizon(d, start, end):
                        key = "pce" if needle.startswith("Personal") else "gdp"
                        items.append({"id": f"bea:{key}:{year}-{mon:02d}-{da:02d}", "source": "BEA", "source_type": "official", "title": needle.rstrip(" ("), "title_zh": zh, "date": hk_date, "end_date": None, "time_hkt": hk_time, "importance": importance, "category": "美国宏观", "impact_classes": ["stock", "gold", "crypto"], "url": url, "note": "美国经济分析局官方发布日程；时间已转换为香港时间。", "system": True})
                pos = idx + len(needle_lower)
        dedup: dict[str, dict[str, Any]] = {x["id"]: x for x in items}
        return list(dedup.values()), None
    except Exception as exc:
        return [], f"BEA 日历失败：{exc}"


def bundled_2026_calendar_fallback(source: str, start: date, end: date) -> list[dict[str, Any]]:
    """Small verified 2026 fallback snapshot so the calendar still works if an official site is temporarily blocked."""
    rows: list[tuple[str, str, str, str | None, str]] = []
    if source == "Federal Reserve":
        rows = [
            ("2026-09-15", "FOMC 议息会议", "FOMC Meeting (Sept. 15-16)", None, "2026-09-16"),
            ("2026-10-07", "FOMC 会议纪要发布", "FOMC Minutes", None, ""),
            ("2026-10-27", "FOMC 议息会议", "FOMC Meeting (Oct. 27-28)", None, "2026-10-28"),
        ]
    elif source == "BLS":
        rows = [
            ("2026-09-04", "美国非农就业报告", "Employment Situation", "20:30", ""),
            ("2026-09-10", "美国 PPI", "Producer Price Index", "20:30", ""),
            ("2026-09-11", "美国 CPI", "Consumer Price Index", "20:30", ""),
            ("2026-09-29", "美国 JOLTS 职位空缺", "Job Openings and Labor Turnover Survey", "22:00", ""),
            ("2026-10-02", "美国非农就业报告", "Employment Situation", "20:30", ""),
            ("2026-10-14", "美国 CPI", "Consumer Price Index", "20:30", ""),
            ("2026-10-15", "美国 PPI", "Producer Price Index", "20:30", ""),
            ("2026-10-30", "美国 ECI 就业成本指数", "Employment Cost Index", "20:30", ""),
        ]
    elif source == "BEA":
        rows = [
            ("2026-08-26", "美国 GDP", "GDP (Second Estimate), Q2 2026", "20:30", ""),
            ("2026-08-26", "美国 PCE / 个人收入与支出", "Personal Income and Outlays, July 2026", "20:30", ""),
            ("2026-09-30", "美国 GDP", "GDP (Third Estimate), Q2 2026", "20:30", ""),
            ("2026-09-30", "美国 PCE / 个人收入与支出", "Personal Income and Outlays, August 2026", "20:30", ""),
            ("2026-10-29", "美国 GDP", "GDP (Advance Estimate), Q3 2026", "20:30", ""),
            ("2026-10-29", "美国 PCE / 个人收入与支出", "Personal Income and Outlays, September 2026", "20:30", ""),
        ]
    items: list[dict[str, Any]] = []
    for ds, zh, en, tm, end_ds in rows:
        d = date.fromisoformat(ds)
        if not _event_in_horizon(d, start, end):
            continue
        items.append({"id": f"fallback:{source}:{ds}:{zh}", "source": source, "source_type": "official_snapshot", "title": en, "title_zh": zh, "date": ds, "end_date": end_ds or None, "time_hkt": tm, "importance": "high", "category": "美联储" if source == "Federal Reserve" else "美国宏观", "impact_classes": ["stock", "gold", "crypto"], "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm" if source == "Federal Reserve" else ("https://www.bls.gov/schedule/" if source == "BLS" else "https://www.bea.gov/news/schedule"), "note": "内置官方日程快照：仅在在线官方源临时不可用时启用。", "system": True})
    return items


def build_economic_calendar(days: int = 62, force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _CALENDAR_CACHE.get("payload") and now - float(_CALENDAR_CACHE.get("time") or 0) < 21600:
        cached = dict(_CALENDAR_CACHE["payload"])
        cached["cached"] = True
        return cached
    start = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    end = start + timedelta(days=max(14, min(int(days or 62), 120)))
    all_items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for name, fn in (("Federal Reserve", fetch_fed_calendar), ("BLS", fetch_bls_calendar), ("BEA", fetch_bea_calendar)):
        items, err = fn(start, end)
        fallback = False
        if not items:
            fallback_items = bundled_2026_calendar_fallback(name, start, end)
            if fallback_items:
                items = fallback_items
                fallback = True
        all_items.extend(items)
        sources.append({"source": name, "ok": bool(items), "count": len(items), "error": err, "fallback": fallback})
    dedup = {str(x.get("id")): x for x in all_items if x.get("id")}
    items = sorted(dedup.values(), key=lambda x: (str(x.get("date") or ""), 0 if x.get("importance") == "high" else 1, str(x.get("time_hkt") or "")))
    payload = {"ok": True, "asof": now_iso(), "cached": False, "range_start": start.isoformat(), "range_end": end.isoformat(), "items": items, "sources": sources}
    _CALENDAR_CACHE.update({"time": now, "payload": payload})
    return payload


def build_important_news(assets: list[dict[str, Any]], force: bool = False) -> dict[str, Any]:
    secrets = load_local_secrets()
    token = str(secrets.get("marketaux_token") or "").strip()
    sig = json.dumps(sorted((str(a.get("ticker") or ""), str(a.get("type") or ""), round(float(a.get("weight_pct") or 0), 2)) for a in assets), ensure_ascii=False)
    cache_key = f"{sig}|marketaux={bool(token)}"
    now = time.time()
    if not force and _NEWS_CACHE.get("key") == cache_key and _NEWS_CACHE.get("payload") and now - float(_NEWS_CACHE.get("time") or 0) < 1800:
        cached = dict(_NEWS_CACHE["payload"])
        cached["cached"] = True
        return cached
    items: list[dict[str, Any]] = []
    status: list[dict[str, Any]] = []
    fed, err = fetch_fed_monetary_news()
    items.extend(fed); status.append({"source": "Federal Reserve", "ok": not bool(err), "count": len(fed), "error": err})
    hkma, err = fetch_hkma_press_news()
    items.extend(hkma); status.append({"source": "HKMA", "ok": not bool(err), "count": len(hkma), "error": err})
    if token:
        mx, err = fetch_marketaux_news(assets, token)
        items.extend(mx); status.append({"source": "Marketaux", "ok": not bool(err), "count": len(mx), "error": err})
    else:
        status.append({"source": "Marketaux", "ok": False, "count": 0, "error": "未配置免费 API Token"})
    # Dedupe by normalized title or URL, keeping the highest score.
    dedup: dict[str, dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("url") or "").strip().lower() or str(item.get("title") or "").strip().lower())
        if not key:
            continue
        if key not in dedup or float(item.get("score") or 0) > float(dedup[key].get("score") or 0):
            dedup[key] = item
    # Keep the news feed deliberately short-lived: old news is context, not a current signal.
    # Future-looking information belongs in the economic-calendar endpoint.
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=7)
    future_grace = now_dt + timedelta(hours=6)
    out: list[dict[str, Any]] = []
    for item in dedup.values():
        published = _parse_datetime_any(item.get("published_at"))
        if published is None or published < cutoff or published > future_grace:
            continue
        out.append(item)
    out.sort(key=lambda x: (float(x.get("score") or 0), str(x.get("published_at") or "")), reverse=True)
    for st in status:
        if st.get("source") == "Marketaux":
            st["count"] = sum(1 for x in out if x.get("source_type") == "aggregator")
        else:
            st["count"] = sum(1 for x in out if x.get("source") == st.get("source"))
    payload = {"ok": True, "asof": now_iso(), "cached": False, "news_window_days": 7, "items": out[:30], "sources": status, "marketaux_configured": bool(token)}
    _NEWS_CACHE.update({"key": cache_key, "time": now, "payload": payload})
    return payload

def fetch_gold_hkd() -> tuple[float | None, dict[str, Any], str | None]:
    urls = ["https://api.frankfurter.dev/v2/rate/XAU/HKD", "https://api.frankfurter.dev/v2/rates?base=XAU&quotes=HKD"]
    errors: list[str] = []
    for url in urls:
        try:
            data = _read_json_url(url)
            row = data[0] if isinstance(data, list) and data else data
            if not isinstance(row, dict):
                raise ValueError("Frankfurter XAU 返回格式异常")
            rate = float(row.get("rate"))
            if rate <= 0:
                raise ValueError("XAU/HKD rate 无效")
            return rate, {"source": "Frankfurter XAU reference", "date": row.get("date"), "note": "国际黄金参考值；不是 HSBC XGT Bank Buy/Bank Sell 或实物金回购价"}, None
        except Exception as exc:
            errors.append(str(exc))
    return None, {}, "黄金参考价失败：" + "; ".join(errors)


def fetch_fx_hkd() -> tuple[dict[str, float], dict[str, Any], str | None]:
    primary = "https://api.frankfurter.dev/v2/rates?base=HKD&quotes=USD,CNY,EUR,GBP,SGD,JPY,AUD,CAD,CHF"
    try:
        rows = _read_json_url(primary)
        rates_hkd: dict[str, float] = {"HKD": 1.0}
        date = None
        if not isinstance(rows, list):
            raise ValueError("Frankfurter 返回格式异常")
        for row in rows:
            quote = str(row.get("quote", "")).upper()
            rate = float(row.get("rate"))
            if rate <= 0:
                continue
            rates_hkd[quote] = 1.0 / rate
            date = date or row.get("date")
        required = ("USD", "CNY", "EUR", "GBP", "SGD", "JPY", "AUD", "CAD", "CHF")
        if not all(k in rates_hkd for k in required):
            missing = ",".join(k for k in required if k not in rates_hkd)
            raise ValueError(f"Frankfurter 未返回完整汇率：{missing}")
        rates_hkd["USDT"] = rates_hkd["USD"]
        return rates_hkd, {"source": "Frankfurter", "date": date, "usdt_note": "USDT 按 USD/HKD 参考汇率折算"}, None
    except Exception as primary_error:
        fallback = "https://open.er-api.com/v6/latest/USD"
        try:
            data = _read_json_url(fallback)
            if data.get("result") != "success":
                raise ValueError(data.get("error-type") or "ExchangeRate-API 返回失败")
            r = data["rates"]
            usd_hkd = float(r["HKD"])
            rates_hkd = {"HKD": 1.0, "USD": usd_hkd, "USDT": usd_hkd, **{k: usd_hkd / float(r[k]) for k in ("CNY","EUR","GBP","SGD","JPY","AUD","CAD","CHF")}}
            return rates_hkd, {"source": "ExchangeRate-API Open", "date": data.get("time_last_update_utc"), "attribution": "https://www.exchangerate-api.com", "usdt_note": "USDT 按 USD/HKD 参考汇率折算"}, None
        except Exception as fallback_error:
            return {}, {}, f"自动汇率失败：Frankfurter={primary_error}; fallback={fallback_error}"





class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        obj = json.loads(raw or "{}")
        if not isinstance(obj, dict):
            raise ValueError("请求必须是 JSON 对象")
        return obj

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state-bootstrap.js":
            state = load_state_from_db()
            js = (
                "window.__PORTFOLIO_DB_STATE__=" + json.dumps(state, ensure_ascii=False) + ";\n"
                "window.__PORTFOLIO_DB_ENABLED__=true;\n"
                "window.__PORTFOLIO_DB_PATH__=" + json.dumps(str(DB_PATH), ensure_ascii=False) + ";\n"
                "window.__PORTFOLIO_BACKUP_DIR__=" + json.dumps(str(BACKUP_DIR), ensure_ascii=False) + ";\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(js)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(js)
            return
        if path == "/api/status":
            self.send_json({"ok": True, "app_version": APP_VERSION, "asof": now_iso(), "longbridge_cli": bool(find_longbridge_executable()), "binance_public": True, "fx_public": True, "gold_reference_public": True, "storage": "sqlite", "database": str(DB_PATH), "backups": str(BACKUP_DIR), "state_exists": load_state_from_db() is not None, "cloud_sync": CLOUD_SYNC.status()})
            return
        if path == "/api/fx":
            rates, meta, err = fetch_fx_hkd()
            self.send_json({"ok": not bool(err), "asof": now_iso(), "rates": rates, "meta": meta, "error": err})
            return
        if path == "/api/gold":
            xau_hkd, meta, err = fetch_gold_hkd()
            self.send_json({"ok": not bool(err), "asof": now_iso(), "xau_hkd": xau_hkd, "meta": meta, "error": err})
            return
        if path == "/api/news-settings":
            secrets = load_local_secrets()
            tok = str(secrets.get("marketaux_token") or "")
            self.send_json({"ok": True, "marketaux_configured": bool(tok), "token_hint": ("…" + tok[-4:]) if len(tok) >= 4 else ("已配置" if tok else "")})
            return
        if path == "/api/cloud/status":
            self.send_json(CLOUD_SYNC.status())
            return
        if path == "/api/cloud/schema":
            schema_path = ROOT / "supabase_setup.sql"
            try:
                text = schema_path.read_text(encoding="utf-8")
                self.send_json({"ok": True, "sql": text})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/macro":
            self.send_json(build_macro_indicators(force=False))
            return
        if path == "/api/economic-calendar":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((qs.get("days") or ["62"])[0])
            except Exception:
                days = 62
            self.send_json(build_economic_calendar(days=days, force=False))
            return
        return super().do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/api/state", "/api/state-beacon"}:
            try:
                payload = self.read_json_body()
                state = payload.get("state", payload)
                if not isinstance(state, dict):
                    raise ValueError("state 必须是对象")
                save_state_to_db(state)
                self.send_json({"ok": True, "saved_at": now_iso()})
            except Exception as e:
                self.send_json({"ok": False, "error": f"保存失败：{e}"}, 400)
            return
        if path == "/api/backup-now":
            try:
                files = force_backup()
                self.send_json({"ok": True, "files": files, "backup_dir": str(BACKUP_DIR)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return
        if path == "/api/open-data-folder":
            try:
                open_path(DATA_DIR)
                self.send_json({"ok": True, "path": str(DATA_DIR)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/export-ai-json":
            try:
                bundle = self.read_json_body()
                stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")
                out = EXPORT_DIR / f"portfolio-control-v{APP_VERSION}-ai-full-{stamp}.json"
                out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
                self.send_json({"ok": True, "file": str(out), "export_dir": str(EXPORT_DIR)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return
        if path == "/api/cloud/configure":
            try:
                payload = self.read_json_body()
                login_result = CLOUD_SYNC.login(
                    str(payload.get("project_url") or payload.get("url") or ""),
                    str(payload.get("publishable_key") or payload.get("anon_key") or ""),
                    str(payload.get("email") or ""),
                    str(payload.get("password") or ""),
                    str(payload.get("sync_passphrase") or ""),
                    signup=bool(payload.get("signup")),
                )
                if login_result.get("needs_confirmation"):
                    login_result["status"] = CLOUD_SYNC.status()
                    self.send_json(login_result)
                    return
                sync_result = CLOUD_SYNC.sync(load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False))
                out = {**login_result, **sync_result}
                out["conflict"] = sync_result.get("action") == "conflict" or bool(sync_result.get("conflicts"))
                out["status"] = CLOUD_SYNC.status()
                self.send_json(out, 200 if sync_result.get("ok") else 400)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return
        if path == "/api/cloud/sync":
            try:
                payload = self.read_json_body()
                action = str(payload.get("action") or "auto").lower()
                if action == "pull":
                    meta_status = CLOUD_SYNC.status()
                    result = (CLOUD_SYNC.resolve("remote", load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False))
                              if meta_status.get("has_conflict") else
                              CLOUD_SYNC.force_pull(load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False)))
                elif action == "push":
                    meta_status = CLOUD_SYNC.status()
                    result = (CLOUD_SYNC.resolve("local", load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False))
                              if meta_status.get("has_conflict") else
                              CLOUD_SYNC.force_push(load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False)))
                else:
                    result = CLOUD_SYNC.sync(load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False))
                result["conflict"] = result.get("action") == "conflict" or bool(result.get("conflicts"))
                result["status"] = CLOUD_SYNC.status()
                self.send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self.send_json({"ok": False, "error": f"云同步失败：{e}"}, 500)
            return
        if path == "/api/cloud/resolve":
            try:
                payload = self.read_json_body()
                result = CLOUD_SYNC.resolve(str(payload.get("strategy") or ""), load_state_from_db, lambda st: save_state_to_db(st, mark_dirty=False))
                self.send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self.send_json({"ok": False, "error": f"冲突处理失败：{e}"}, 500)
            return
        if path in {"/api/cloud/disable", "/api/cloud/disconnect"}:
            try:
                CLOUD_SYNC.disable()
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/news-settings":
            try:
                payload = self.read_json_body()
                token = str(payload.get("marketaux_token") or "").strip()
                secrets = load_local_secrets()
                if token:
                    secrets["marketaux_token"] = token
                else:
                    secrets.pop("marketaux_token", None)
                save_local_secrets(secrets)
                _NEWS_CACHE.update({"key": None, "time": 0.0, "payload": None})
                self.send_json({"ok": True, "marketaux_configured": bool(token), "token_hint": ("…" + token[-4:]) if len(token) >= 4 else ("已配置" if token else "")})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return
        if path == "/api/news":
            try:
                payload = self.read_json_body()
                assets = payload.get("assets") or []
                if not isinstance(assets, list):
                    raise ValueError("assets 必须是数组")
                self.send_json(build_important_news(assets, bool(payload.get("force"))))
            except Exception as e:
                self.send_json({"ok": False, "error": f"新闻获取失败：{e}"}, 500)
            return
        if path == "/api/translate":
            try:
                payload = self.read_json_body()
                texts = payload.get("texts")
                if texts is None:
                    texts = [payload.get("text") or ""]
                if not isinstance(texts, list):
                    raise ValueError("texts 必须是数组")
                results = []
                for text in texts[:4]:
                    translated, err = translate_en_to_zh(str(text or ""))
                    results.append({"text": str(text or ""), "translated": translated, "error": err})
                self.send_json({"ok": not any(x.get("error") for x in results), "results": results})
            except Exception as e:
                self.send_json({"ok": False, "error": f"翻译失败：{e}"}, 500)
            return
        if path == "/api/macro":
            try:
                payload = self.read_json_body()
                self.send_json(build_macro_indicators(force=bool(payload.get("force"))))
            except Exception as e:
                self.send_json({"ok": False, "error": f"宏观指标获取失败：{e}"}, 500)
            return
        if path == "/api/economic-calendar":
            try:
                payload = self.read_json_body()
                days = int(payload.get("days") or 62)
                self.send_json(build_economic_calendar(days=days, force=bool(payload.get("force"))))
            except Exception as e:
                self.send_json({"ok": False, "error": f"财经日历获取失败：{e}"}, 500)
            return
        if path != "/api/quotes":
            self.send_json({"error": "Not found"}, 404)
            return

        try:
            payload = self.read_json_body()
            assets = payload.get("assets", [])
            needs_gold = bool(payload.get("needs_gold"))
            if not isinstance(assets, list):
                raise ValueError("assets 必须是数组")
        except (ValueError, json.JSONDecodeError) as e:
            self.send_json({"error": f"请求格式错误：{e}"}, 400)
            return

        stock_requests: list[tuple[dict[str, Any], str]] = []
        crypto_requests: list[tuple[dict[str, Any], str]] = []
        pre_errors: list[dict[str, Any]] = []
        for a in assets:
            typ = a.get("type")
            try:
                if typ == "stock":
                    stock_requests.append((a, normalize_longbridge_symbol(a.get("ticker", ""))))
                elif typ == "crypto":
                    crypto_requests.append((a, normalize_binance_symbol(a.get("ticker", ""))))
                else:
                    pre_errors.append({"id": a.get("id"), "ok": False, "error": "该资产类型暂不支持自动行情"})
            except ValueError as e:
                pre_errors.append({"id": a.get("id"), "ok": False, "error": str(e)})

        unique_stocks = list(dict.fromkeys(sym for _, sym in stock_requests))
        lb_quotes, lb_error = fetch_longbridge(unique_stocks)
        results: list[dict[str, Any]] = pre_errors[:]
        asof = now_iso()
        for a, sym in stock_requests:
            q = lb_quotes.get(sym)
            if q:
                results.append({"id": a.get("id"), "ok": True, "price": q["price"], "symbol": sym, "currency": stock_currency(sym), "source": "Longbridge", "asof": asof, "session": q.get("session"), "quote_time": q.get("quote_time"), "regular_price": q.get("regular_price"), "prev_close": q.get("prev_close"), "status": q.get("status")})
            else:
                results.append({"id": a.get("id"), "ok": False, "symbol": sym, "error": lb_error or "Longbridge 未返回该代码行情"})
        # Quote identical crypto symbols once. This matters for Watchlist items:
        # an unheld symbol and a held symbol can be requested in the same refresh.
        crypto_cache: dict[str, tuple[float | None, str | None]] = {}
        for a, sym in crypto_requests:
            if sym not in crypto_cache:
                crypto_cache[sym] = fetch_binance(sym)
            price, err = crypto_cache[sym]
            if price is not None:
                source = "USDT 基准" if sym == "USDT" else "Binance"
                results.append({"id": a.get("id"), "ok": True, "price": price, "symbol": sym, "currency": "USDT", "source": source, "asof": asof, "session": "24/7"})
            else:
                results.append({"id": a.get("id"), "ok": False, "symbol": sym, "error": err or "Binance 未返回行情"})

        fx_rates, fx_meta, fx_error = fetch_fx_hkd()
        gold_rate, gold_meta, gold_error = (fetch_gold_hkd() if needs_gold else (None, {}, None))
        self.send_json({"ok": True, "asof": asof, "results": results, "fx": {"ok": not bool(fx_error), "rates": fx_rates, "meta": fx_meta, "error": fx_error}, "gold": {"ok": (not bool(gold_error)) if needs_gold else True, "needed": needs_gold, "xau_hkd": gold_rate, "meta": gold_meta, "error": gold_error}})


def start_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    ensure_storage()
    state = load_state_from_db()
    if state is not None:
        try:
            write_daily_backup(state)
        except Exception as exc:
            print(f"WARNING: startup backup failed: {exc}")
    last_error: OSError | None = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            threading.Thread(target=server.serve_forever, daemon=True, name="PortfolioControlHTTP").start()
            return server, candidate
        except OSError as exc:
            last_error = exc
    raise OSError(f"无法找到可用本地端口：{last_error}")


def find_edge_executable() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for p in candidates:
        if str(p) and p.exists():
            return str(p)
    return shutil.which("msedge.exe")


def run_edge_app(url: str, server: ThreadingHTTPServer) -> bool:
    edge = find_edge_executable()
    if not edge:
        return False

    # Use a per-launch profile. Reusing one profile can make Edge hand the app
    # window to an already-running background browser process and then exit the
    # child we are waiting on, which is unreliable for desktop lifecycle tracking.
    session_dir = DATA_DIR / "edge_sessions" / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    print("Launching Microsoft Edge app window...")
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = subprocess.Popen([
            edge,
            f"--app={url}",
            f"--user-data-dir={session_dir}",
            "--no-first-run",
            "--disable-background-mode",
            "--disable-extensions",
            "--disable-features=msEdgeFirstRunExperience,msStartupBoost,StartupBoost",
        ])
        proc.wait()
    finally:
        _close_server(server)
        # Edge can keep harmless files locked briefly after the app window closes.
        # Cleanup is best-effort and never blocks the Portfolio Control shutdown.
        for _ in range(6):
            try:
                shutil.rmtree(session_dir, ignore_errors=False)
                break
            except OSError:
                time.sleep(0.2)
        else:
            shutil.rmtree(session_dir, ignore_errors=True)
    return True

def run_desktop(url: str, server: ThreadingHTTPServer) -> int:
    # pywebview gives the cleanest native window when available.  It is optional:
    # some new Python builds may not yet have compatible binary dependencies.
    try:
        import webview  # type: ignore
    except Exception as exc:
        print(f"pywebview unavailable: {exc}")
        if run_edge_app(url, server):
            return 0
        print("Falling back to the system browser.")
        webbrowser.open(url)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            server.shutdown()
            server.server_close()
        return 0

    print("Launching pywebview desktop window...")
    window = webview.create_window(APP_NAME, url, width=1380, height=900, min_size=(980, 680), confirm_close=False)
    try:
        # EdgeChromium/WebView2 is the preferred Windows backend.
        if sys.platform.startswith("win"):
            webview.start(debug=False, gui="edgechromium")
        else:
            webview.start(debug=False)
    except Exception as exc:
        print(f"pywebview failed to start: {exc}")
        if run_edge_app(url, server):
            return 0
        print("Falling back to the system browser.")
        webbrowser.open(url)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
    finally:
        _close_server(server)
    return 0



def acquire_single_instance() -> bool:
    """Prevent concurrent desktop instances, while recovering stuck prior runs."""
    global _INSTANCE_MUTEX_HANDLE
    if not sys.platform.startswith("win"):
        return True

    def _try_create() -> tuple[int | None, int]:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, "Local\\PortfolioControlDesktopMutex")
        if not handle:
            return None, ctypes.get_last_error()
        return int(handle), ctypes.get_last_error()

    try:
        handle, error = _try_create()
        if handle and error != 183:  # ERROR_ALREADY_EXISTS
            _INSTANCE_MUTEX_HANDLE = handle
            _write_instance_pid()
            return True

        if handle:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))
            except Exception:
                pass

        old_pid = _read_instance_pid()
        # Make the recovery prompt visible even when the executable has no console.
        MB_YESNO = 0x00000004
        MB_ICONWARNING = 0x00000030
        MB_SETFOREGROUND = 0x00010000
        MB_TOPMOST = 0x00040000
        IDYES = 6
        msg = (
            "检测到上一次 Portfolio Control 仍在后台运行。\n\n"
            "这通常是桌面窗口已经关闭，但 Edge/WebView 后台进程没有及时退出。\n"
            "是否结束旧实例并重新启动？"
        )
        answer = ctypes.windll.user32.MessageBoxW(
            None, msg, APP_NAME, MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
        )
        if answer != IDYES:
            return False
        if old_pid and _terminate_process_windows(old_pid):
            time.sleep(0.5)
            try:
                _INSTANCE_PID_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            handle, error = _try_create()
            if handle and error != 183:
                _INSTANCE_MUTEX_HANDLE = handle
                _write_instance_pid()
                return True
            if handle:
                try:
                    ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))
                except Exception:
                    pass

        ctypes.windll.user32.MessageBoxW(
            None,
            "旧实例仍未能退出。请在任务管理器结束 PortfolioControl.exe 后再启动。",
            APP_NAME,
            0x40 | MB_SETFOREGROUND | MB_TOPMOST,
        )
        return False
    except Exception as exc:
        print(f"WARNING: single-instance check unavailable: {exc}")
        return True

def _main_impl() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser", action="store_true", help="Use the system browser instead of desktop WebView")
    parser.add_argument("--no-browser", action="store_true", help="Start server only")
    args = parser.parse_args()

    if not args.no_browser and not acquire_single_instance():
        return

    if args.no_browser:
        ensure_storage()
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"{APP_NAME} v{APP_VERSION} server: http://127.0.0.1:{args.port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    if find_longbridge_executable():
        ensure_longbridge_authorized_first_run()

    server, actual_port = start_server(args.host, args.port)
    url = f"http://127.0.0.1:{actual_port}/"
    print("=" * 64)
    print(f"{APP_NAME} v{APP_VERSION}")
    print(f"Database: {DB_PATH}")
    print(f"Backups:  {BACKUP_DIR}")
    print(f"Longbridge CLI: {'FOUND' if find_longbridge_executable() else 'NOT FOUND'}")
    print("=" * 64)

    if args.browser:
        webbrowser.open(url)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            server.shutdown()
            server.server_close()
        return
    raise SystemExit(run_desktop(url, server))


def main() -> None:
    try:
        _main_impl()
    finally:
        # Explicit release makes close/reopen reliable even before interpreter teardown.
        release_single_instance()


if __name__ == "__main__":
    main()
