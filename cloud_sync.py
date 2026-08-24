from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    CRYPTO_AVAILABLE = True
except Exception:
    hashes = AESGCM = PBKDF2HMAC = None  # type: ignore
    CRYPTO_AVAILABLE = False

SYNC_SCHEMA = "portfolio-control-cloud-v1"
AAD = b"portfolio-control-cloud-v1"
KDF_ITERATIONS = 320_000
QUOTE_KEYS = {
    "lastQuoteAt", "lastQuoteSource", "quoteSymbol", "quoteSession", "quoteTime", "quoteError",
    "lastPrice", "quoteCurrency",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(stable_json(obj).encode("utf-8")).hexdigest()


def _clean_asset(asset: dict[str, Any]) -> dict[str, Any]:
    a = copy.deepcopy(asset)
    for k in QUOTE_KEYS:
        a.pop(k, None)
    if a.get("priceMode") == "auto" and a.get("type") in {"stock", "crypto"}:
        a.pop("price", None)
    return a


def _clean_watch(item: dict[str, Any]) -> dict[str, Any]:
    w = copy.deepcopy(item)
    for k in QUOTE_KEYS:
        w.pop(k, None)
    return w


def state_to_core(state: dict[str, Any]) -> dict[str, Any]:
    """Strip volatile market/news fields before encrypted cloud sync.

    Prices and news refresh independently on each device.  User-authored portfolio
    data, risk rules, calendar, notes, decisions, watchlist definitions, and
    snapshots are synchronized.
    """
    core: dict[str, Any] = {
        "settings": copy.deepcopy(state.get("settings") or {}),
        "targets": copy.deepcopy(state.get("targets") or {}),
        "assets": [_clean_asset(x) for x in (state.get("assets") or []) if isinstance(x, dict)],
        "snapshots": copy.deepcopy(state.get("snapshots") or []),
        "workspace": copy.deepcopy(state.get("workspace") or {}),
    }
    ws = core.get("workspace") or {}
    if isinstance(ws, dict) and isinstance(ws.get("watchlist"), list):
        ws["watchlist"] = [_clean_watch(x) for x in ws["watchlist"] if isinstance(x, dict)]
    if str((state.get("settings") or {}).get("fxMode") or "auto") == "manual":
        core["fx"] = copy.deepcopy(state.get("fx") or {})
    return core


def apply_core(local_state: dict[str, Any] | None, core: dict[str, Any]) -> dict[str, Any]:
    """Apply synchronized user data while preserving device-local volatile quotes/news.

    Auto market prices are refreshed independently on each computer. Pulling from
    the cloud therefore must not blank a perfectly good local quote while the
    next refresh is pending.
    """
    out = copy.deepcopy(local_state or {})
    for key in ("settings", "targets", "snapshots"):
        if key in core:
            out[key] = copy.deepcopy(core[key])

    if "assets" in core:
        local_assets = {str(x.get("id")): x for x in (out.get("assets") or []) if isinstance(x, dict) and x.get("id")}
        merged_assets: list[dict[str, Any]] = []
        for item in (core.get("assets") or []):
            if not isinstance(item, dict):
                continue
            a = copy.deepcopy(item)
            old = local_assets.get(str(a.get("id")))
            if old:
                for k in QUOTE_KEYS:
                    if k in old and k not in a:
                        a[k] = copy.deepcopy(old[k])
                if a.get("priceMode") == "auto" and a.get("type") in {"stock", "crypto"} and "price" not in a and "price" in old:
                    a["price"] = old["price"]
            merged_assets.append(a)
        out["assets"] = merged_assets

    if "workspace" in core:
        ws = copy.deepcopy(core.get("workspace") or {})
        old_ws = out.get("workspace") or {}
        old_watch = {str(x.get("id")): x for x in (old_ws.get("watchlist") or []) if isinstance(x, dict) and x.get("id")} if isinstance(old_ws, dict) else {}
        if isinstance(ws, dict) and isinstance(ws.get("watchlist"), list):
            new_watch = []
            for item in ws["watchlist"]:
                if not isinstance(item, dict):
                    continue
                w = copy.deepcopy(item); old = old_watch.get(str(w.get("id")))
                if old:
                    for k in QUOTE_KEYS:
                        if k in old and k not in w:
                            w[k] = copy.deepcopy(old[k])
                new_watch.append(w)
            ws["watchlist"] = new_watch
        out["workspace"] = ws

    if "fx" in core:
        out["fx"] = copy.deepcopy(core["fx"])
    return out


def _list_key(items: list[Any]) -> str | None:
    dicts = [x for x in items if isinstance(x, dict)]
    if len(dicts) != len(items) or not dicts:
        return None
    for key in ("id", "date"):
        vals = [str(x.get(key, "")) for x in dicts]
        if all(vals) and len(set(vals)) == len(vals):
            return key
    return None


def three_way_merge(base: Any, local: Any, remote: Any, path: str = "") -> tuple[Any, list[str]]:
    """Merge non-overlapping edits; report paths changed differently on both devices."""
    if local == remote:
        return copy.deepcopy(local), []
    if local == base:
        return copy.deepcopy(remote), []
    if remote == base:
        return copy.deepcopy(local), []

    if isinstance(base, dict) and isinstance(local, dict) and isinstance(remote, dict):
        out: dict[str, Any] = {}
        conflicts: list[str] = []
        missing = object()
        for key in sorted(set(base) | set(local) | set(remote)):
            b = base.get(key, missing); l = local.get(key, missing); r = remote.get(key, missing)
            p = f"{path}.{key}" if path else str(key)
            if l is missing and r is missing:
                continue
            if l is missing:
                if b is missing or r == b:
                    continue
                conflicts.append(p); out[key] = copy.deepcopy(r); continue
            if r is missing:
                if b is missing or l == b:
                    continue
                conflicts.append(p); out[key] = copy.deepcopy(l); continue
            if b is missing:
                if l == r:
                    out[key] = copy.deepcopy(l)
                else:
                    conflicts.append(p); out[key] = copy.deepcopy(l)
                continue
            merged, c = three_way_merge(b, l, r, p)
            out[key] = merged; conflicts.extend(c)
        return out, conflicts

    if isinstance(base, list) and isinstance(local, list) and isinstance(remote, list):
        key = _list_key(base) or _list_key(local) or _list_key(remote)
        if key:
            bm = {str(x[key]): x for x in base if isinstance(x, dict) and key in x}
            lm = {str(x[key]): x for x in local if isinstance(x, dict) and key in x}
            rm = {str(x[key]): x for x in remote if isinstance(x, dict) and key in x}
            order: list[str] = []
            for seq in (local, remote, base):
                for x in seq:
                    if isinstance(x, dict) and key in x and str(x[key]) not in order:
                        order.append(str(x[key]))
            out_list: list[Any] = []
            conflicts: list[str] = []
            missing = object()
            for ident in order:
                b = bm.get(ident, missing); l = lm.get(ident, missing); r = rm.get(ident, missing)
                p = f"{path}[{key}={ident}]"
                if l is missing and r is missing:
                    continue
                if l is missing:
                    if b is missing or r == b:
                        if b is missing:
                            out_list.append(copy.deepcopy(r))
                        continue
                    conflicts.append(p); out_list.append(copy.deepcopy(r)); continue
                if r is missing:
                    if b is missing or l == b:
                        if b is missing:
                            out_list.append(copy.deepcopy(l))
                        continue
                    conflicts.append(p); out_list.append(copy.deepcopy(l)); continue
                if b is missing:
                    if l == r:
                        out_list.append(copy.deepcopy(l))
                    else:
                        conflicts.append(p); out_list.append(copy.deepcopy(l))
                    continue
                merged, c = three_way_merge(b, l, r, p)
                out_list.append(merged); conflicts.extend(c)
            return out_list, conflicts

    return copy.deepcopy(local), [path or "portfolio"]


class CloudSyncManager:
    def __init__(self, data_dir: Path, db_path: Path, secrets_path: Path, app_version: str):
        self.data_dir = data_dir
        self.db_path = db_path
        self.secrets_path = secrets_path
        self.app_version = app_version
        self.lock = threading.RLock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cloud_sync_meta (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    device_id TEXT NOT NULL,
                    last_cloud_revision INTEGER NOT NULL DEFAULT 0,
                    base_core_json TEXT,
                    last_sync_at TEXT,
                    last_status TEXT,
                    pending_remote_core_json TEXT,
                    pending_remote_revision INTEGER,
                    pending_conflicts_json TEXT
                )
            """)
            row = conn.execute("SELECT id FROM cloud_sync_meta WHERE id=1").fetchone()
            if not row:
                conn.execute("INSERT INTO cloud_sync_meta(id,device_id,last_cloud_revision,last_status) VALUES(1,?,?,?)", (str(uuid.uuid4()), 0, "未配置"))
            conn.commit()

    def _meta(self) -> dict[str, Any]:
        self.ensure_schema()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT device_id,last_cloud_revision,base_core_json,last_sync_at,last_status,pending_remote_core_json,pending_remote_revision,pending_conflicts_json FROM cloud_sync_meta WHERE id=1").fetchone()
        if not row:
            return {}
        keys = ["device_id","last_cloud_revision","base_core_json","last_sync_at","last_status","pending_remote_core_json","pending_remote_revision","pending_conflicts_json"]
        return dict(zip(keys, row))

    def _update_meta(self, **values: Any) -> None:
        if not values:
            return
        allowed = {"device_id","last_cloud_revision","base_core_json","last_sync_at","last_status","pending_remote_core_json","pending_remote_revision","pending_conflicts_json"}
        values = {k: v for k, v in values.items() if k in allowed}
        cols = ",".join(f"{k}=?" for k in values)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE cloud_sync_meta SET {cols} WHERE id=1", tuple(values.values()))
            conn.commit()

    def _secrets(self) -> dict[str, Any]:
        try:
            obj = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _save_secrets(self, obj: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.secrets_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.secrets_path)
        try:
            os.chmod(self.secrets_path, 0o600)
        except Exception:
            pass

    def _cfg(self) -> dict[str, Any]:
        return dict((self._secrets().get("cloud_sync") or {}))

    def _save_cfg(self, cfg: dict[str, Any]) -> None:
        sec = self._secrets(); sec["cloud_sync"] = cfg; self._save_secrets(sec)

    def configured(self) -> bool:
        c = self._cfg()
        return bool(c.get("project_url") and c.get("publishable_key") and c.get("refresh_token") and c.get("sync_passphrase") and c.get("user_id"))

    def _headers(self, access: str | None = None, prefer: str | None = None) -> dict[str, str]:
        c = self._cfg(); key = str(c.get("publishable_key") or "")
        h = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"Portfolio-Control/{self.app_version}"}
        token = access or c.get("access_token")
        if token:
            h["Authorization"] = f"Bearer {token}"
        if prefer:
            h["Prefer"] = prefer
        return h

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        base = str(self._cfg().get("project_url") or "").rstrip("/")
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _request(self, method: str, path: str, body: Any = None, query: dict[str, str] | None = None, auth: bool = True, prefer: str | None = None, retry_auth: bool = True) -> Any:
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = self._headers(prefer=prefer)
        if not auth:
            headers.pop("Authorization", None)
        req = urllib.request.Request(self._url(path, query), data=raw, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                if not data:
                    return None
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 401 and auth and retry_auth and self.refresh_session():
                return self._request(method, path, body, query, auth, prefer, retry_auth=False)
            try:
                detail = json.loads(payload)
                msg = detail.get("msg") or detail.get("message") or detail.get("error_description") or detail.get("error") or payload
            except Exception:
                msg = payload
            raise RuntimeError(f"Supabase HTTP {exc.code}: {msg}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 Supabase：{exc.reason}") from exc

    def login(self, project_url: str, publishable_key: str, email: str, password: str, sync_passphrase: str, signup: bool = False) -> dict[str, Any]:
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("当前程序未包含 cryptography，无法启用加密云同步。请使用 v3.5 正式构建版。")
        project_url = project_url.strip().rstrip("/")
        publishable_key = publishable_key.strip(); email = email.strip(); password = password.strip(); sync_passphrase = sync_passphrase.strip()
        if not project_url.startswith("https://") or not publishable_key or not email or len(password) < 6 or len(sync_passphrase) < 8:
            raise ValueError("请填写 Supabase HTTPS Project URL、Publishable/anon key、邮箱、账户密码，以及至少 8 位同步加密密码。")
        old = self._cfg()
        temp = {**old, "project_url": project_url, "publishable_key": publishable_key, "email": email, "sync_passphrase": sync_passphrase}
        self._save_cfg(temp)
        try:
            endpoint = "/auth/v1/signup" if signup else "/auth/v1/token"
            query = None if signup else {"grant_type": "password"}
            resp = self._request("POST", endpoint, {"email": email, "password": password}, query=query, auth=False, retry_auth=False)
            if signup and not resp.get("access_token"):
                # Email confirmation may be required. Keep the project settings but not a half-session.
                temp.pop("access_token", None); temp.pop("refresh_token", None); temp.pop("user_id", None)
                self._save_cfg(temp)
                return {"ok": True, "needs_confirmation": True, "message": "注册成功。Supabase 当前要求验证邮箱，请先打开验证邮件，再回到这里点“连接”。"}
            user = resp.get("user") or {}
            temp.update({"access_token": resp.get("access_token"), "refresh_token": resp.get("refresh_token"), "user_id": user.get("id")})
            if not temp.get("user_id"):
                raise RuntimeError("Supabase 登录成功但未返回 user id。")
            self._save_cfg(temp)
            # Verify the table/RLS configuration now, so setup problems are actionable.
            self.fetch_remote_row()
            self._update_meta(last_status="已连接，等待首次同步")
            return {"ok": True, "needs_confirmation": False, "user_id": temp["user_id"], "email": email}
        except Exception:
            # Keep non-secret endpoint/key/email inputs, but do not overwrite a prior working session on failure.
            if old.get("refresh_token"):
                self._save_cfg(old)
            raise

    def refresh_session(self) -> bool:
        c = self._cfg(); token = str(c.get("refresh_token") or "")
        if not token:
            return False
        try:
            resp = self._request("POST", "/auth/v1/token", {"refresh_token": token}, query={"grant_type": "refresh_token"}, auth=False, retry_auth=False)
            if not resp or not resp.get("access_token"):
                return False
            c["access_token"] = resp.get("access_token"); c["refresh_token"] = resp.get("refresh_token") or token
            if (resp.get("user") or {}).get("id"):
                c["user_id"] = resp["user"]["id"]
            self._save_cfg(c)
            return True
        except Exception:
            return False

    def record_error(self, message: str) -> None:
        try:
            self._update_meta(last_status=f"同步失败：{message}")
        except Exception:
            pass

    def disable(self) -> None:
        sec = self._secrets(); sec.pop("cloud_sync", None); self._save_secrets(sec)
        self._update_meta(last_cloud_revision=0, base_core_json=None, last_sync_at=None, last_status="未配置", pending_remote_core_json=None, pending_remote_revision=None, pending_conflicts_json=None)

    def status(self) -> dict[str, Any]:
        c = self._cfg(); m = self._meta()
        conflicts: list[str] = []
        try:
            conflicts = json.loads(m.get("pending_conflicts_json") or "[]")
        except Exception:
            pass
        dirty = False
        if self.configured():
            try:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute("SELECT state_json FROM app_state WHERE id=1").fetchone()
                if row:
                    local_state = json.loads(row[0])
                    local_core = state_to_core(local_state if isinstance(local_state, dict) else {})
                    base_raw = m.get("base_core_json")
                    if base_raw:
                        base = json.loads(base_raw)
                        dirty = sha256_obj(local_core) != sha256_obj(base)
                    else:
                        dirty = bool(local_core.get("assets") or local_core.get("snapshots") or any((local_core.get("workspace") or {}).get(k) for k in ("calendarItems","memos","decisions","watchlist")))
            except Exception:
                dirty = False
        status_text = str(m.get("last_status") or ("已配置" if self.configured() else "未配置"))
        state = "conflict" if conflicts else ("ready" if self.configured() else "disabled")
        if "失败" in status_text or "错误" in status_text:
            state = "error"
        return {
            "ok": True,
            "available": CRYPTO_AVAILABLE,
            "configured": self.configured(),
            "project_url": c.get("project_url") or "",
            "url": c.get("project_url") or "",
            "email": c.get("email") or "",
            "key_hint": ("…" + str(c.get("publishable_key"))[-6:]) if c.get("publishable_key") else "",
            "device_id": m.get("device_id"),
            "last_cloud_revision": int(m.get("last_cloud_revision") or 0),
            "last_revision": int(m.get("last_cloud_revision") or 0),
            "last_sync_at": m.get("last_sync_at"),
            "dirty": dirty,
            "has_conflict": bool(conflicts),
            "conflict": bool(conflicts),
            "conflicts": conflicts[:20],
            "status_text": status_text,
            "status": {"state": state, "message": status_text, "last_error": status_text if state == "error" else ""},
        }

    def fetch_remote_row(self) -> dict[str, Any] | None:
        c = self._cfg(); uid = str(c.get("user_id") or "")
        if not uid:
            raise RuntimeError("云同步尚未登录。")
        try:
            rows = self._request("GET", "/rest/v1/portfolio_sync", query={"user_id": f"eq.{uid}", "select": "user_id,revision,ciphertext,nonce,kdf_salt,state_hash,device_id,app_version,updated_at", "limit": "1"})
        except RuntimeError as exc:
            msg = str(exc)
            if "42P01" in msg or "does not exist" in msg.lower() or "portfolio_sync" in msg and "404" in msg:
                raise RuntimeError("Supabase 尚未建立 portfolio_sync 表。请在 Supabase SQL Editor 执行程序附带的 supabase_setup.sql。") from exc
            raise
        return rows[0] if isinstance(rows, list) and rows else None

    def _derive_key(self, passphrase: str, salt: bytes) -> bytes:
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("加密组件不可用")
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS)
        return kdf.derive(passphrase.encode("utf-8"))

    def encrypt_core(self, core: dict[str, Any], salt_b64: str | None = None) -> tuple[str, str, str]:
        salt = base64.b64decode(salt_b64) if salt_b64 else os.urandom(16)
        nonce = os.urandom(12); key = self._derive_key(str(self._cfg().get("sync_passphrase") or ""), salt)
        raw = zlib.compress(stable_json(core).encode("utf-8"), level=9)
        ct = AESGCM(key).encrypt(nonce, raw, AAD)
        return base64.b64encode(ct).decode(), base64.b64encode(nonce).decode(), base64.b64encode(salt).decode()

    def decrypt_row(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            salt = base64.b64decode(row["kdf_salt"]); nonce = base64.b64decode(row["nonce"]); ct = base64.b64decode(row["ciphertext"])
            key = self._derive_key(str(self._cfg().get("sync_passphrase") or ""), salt)
            raw = AESGCM(key).decrypt(nonce, ct, AAD)
            obj = json.loads(zlib.decompress(raw).decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("cloud payload is not an object")
            return obj
        except Exception as exc:
            raise RuntimeError("无法解密云端数据。请确认两台电脑填写的是同一个“同步加密密码”。") from exc

    def _insert_remote(self, core: dict[str, Any]) -> dict[str, Any]:
        c = self._cfg(); m = self._meta(); ct, nonce, salt = self.encrypt_core(core)
        row = {"user_id": c["user_id"], "revision": 1, "ciphertext": ct, "nonce": nonce, "kdf_salt": salt, "state_hash": sha256_obj(core), "device_id": m.get("device_id"), "app_version": self.app_version}
        rows = self._request("POST", "/rest/v1/portfolio_sync", row, prefer="return=representation")
        return (rows[0] if isinstance(rows, list) and rows else row)

    def _update_remote(self, core: dict[str, Any], expected_revision: int, salt_b64: str) -> dict[str, Any] | None:
        c = self._cfg(); m = self._meta(); ct, nonce, salt = self.encrypt_core(core, salt_b64)
        body = {"revision": expected_revision + 1, "ciphertext": ct, "nonce": nonce, "kdf_salt": salt, "state_hash": sha256_obj(core), "device_id": m.get("device_id"), "app_version": self.app_version, "updated_at": now_iso()}
        rows = self._request("PATCH", "/rest/v1/portfolio_sync", body, query={"user_id": f"eq.{c['user_id']}", "revision": f"eq.{expected_revision}"}, prefer="return=representation")
        return rows[0] if isinstance(rows, list) and rows else None

    def _accept_synced(self, core: dict[str, Any], revision: int, status: str) -> None:
        self._update_meta(last_cloud_revision=revision, base_core_json=stable_json(core), last_sync_at=now_iso(), last_status=status, pending_remote_core_json=None, pending_remote_revision=None, pending_conflicts_json=None)

    def sync(self, load_state: Callable[[], dict[str, Any] | None], save_state: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self.lock:
            if not self.configured():
                return {"ok": False, "error": "云同步尚未配置。"}
            local_state = load_state() or {}
            local_core = state_to_core(local_state)
            meta = self._meta()
            try:
                base_core = json.loads(meta.get("base_core_json") or "null")
            except Exception:
                base_core = None
            remote_row = self.fetch_remote_row()
            if remote_row is None:
                row = self._insert_remote(local_core)
                rev = int(row.get("revision") or 1)
                self._accept_synced(local_core, rev, "已上传到云端")
                return {"ok": True, "action": "uploaded", "revision": rev, "asof": now_iso()}

            remote_rev = int(remote_row.get("revision") or 0)
            remote_core = self.decrypt_row(remote_row)
            if base_core is None:
                # First sync on this device. If local has user data, do not silently overwrite it.
                has_local = bool(local_core.get("assets") or local_core.get("snapshots") or any((local_core.get("workspace") or {}).get(k) for k in ("calendarItems","memos","decisions","watchlist")))
                if has_local and sha256_obj(local_core) != sha256_obj(remote_core):
                    conflicts = ["首次同步：本机与云端均已有数据"]
                    self._update_meta(last_status="等待冲突处理", pending_remote_core_json=stable_json(remote_core), pending_remote_revision=remote_rev, pending_conflicts_json=json.dumps(conflicts, ensure_ascii=False))
                    return {"ok": True, "action": "conflict", "revision": remote_rev, "conflicts": conflicts}
                new_state = apply_core(local_state, remote_core)
                save_state(new_state)
                self._accept_synced(remote_core, remote_rev, "已从云端同步")
                return {"ok": True, "action": "pulled", "revision": remote_rev, "state": new_state, "asof": now_iso()}

            local_changed = sha256_obj(local_core) != sha256_obj(base_core)
            remote_changed = sha256_obj(remote_core) != sha256_obj(base_core)
            if not local_changed and not remote_changed:
                self._accept_synced(base_core, remote_rev, "云端已是最新")
                return {"ok": True, "action": "noop", "revision": remote_rev, "asof": now_iso()}
            if not local_changed and remote_changed:
                new_state = apply_core(local_state, remote_core); save_state(new_state)
                self._accept_synced(remote_core, remote_rev, "已拉取云端更新")
                return {"ok": True, "action": "pulled", "revision": remote_rev, "state": new_state, "asof": now_iso()}
            if local_changed and not remote_changed:
                updated = self._update_remote(local_core, remote_rev, str(remote_row.get("kdf_salt") or ""))
                if updated is None:
                    # Another device won the race; retry from a fresh remote row.
                    return self.sync(load_state, save_state)
                rev = int(updated.get("revision") or (remote_rev + 1)); self._accept_synced(local_core, rev, "本机更新已同步")
                return {"ok": True, "action": "uploaded", "revision": rev, "asof": now_iso()}

            merged, conflicts = three_way_merge(base_core, local_core, remote_core)
            if conflicts:
                self._update_meta(last_status="等待冲突处理", pending_remote_core_json=stable_json(remote_core), pending_remote_revision=remote_rev, pending_conflicts_json=json.dumps(conflicts[:100], ensure_ascii=False))
                return {"ok": True, "action": "conflict", "revision": remote_rev, "conflicts": conflicts[:20]}
            new_state = apply_core(local_state, merged); save_state(new_state)
            updated = self._update_remote(merged, remote_rev, str(remote_row.get("kdf_salt") or ""))
            if updated is None:
                return self.sync(load_state, save_state)
            rev = int(updated.get("revision") or remote_rev + 1); self._accept_synced(merged, rev, "已自动合并两台设备的不同修改")
            return {"ok": True, "action": "merged", "revision": rev, "state": new_state, "asof": now_iso()}

    def force_pull(self, load_state: Callable[[], dict[str, Any] | None], save_state: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Explicitly replace synchronized core data with the current cloud version."""
        with self.lock:
            if not self.configured():
                return {"ok": False, "error": "云同步尚未配置。"}
            row = self.fetch_remote_row()
            if not row:
                return {"ok": False, "error": "云端还没有 Portfolio Control 数据。请先从有数据的电脑上传。"}
            core = self.decrypt_row(row)
            local_state = load_state() or {}
            new_state = apply_core(local_state, core)
            save_state(new_state)
            rev = int(row.get("revision") or 0)
            self._accept_synced(core, rev, "已手动采用云端版本")
            return {"ok": True, "action": "pulled", "state": new_state, "revision": rev, "asof": now_iso()}

    def force_push(self, load_state: Callable[[], dict[str, Any] | None], save_state: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Explicitly make this device's synchronized core the cloud version."""
        with self.lock:
            if not self.configured():
                return {"ok": False, "error": "云同步尚未配置。"}
            local_state = load_state() or {}
            core = state_to_core(local_state)
            row = self.fetch_remote_row()
            if row is None:
                created = self._insert_remote(core)
                rev = int(created.get("revision") or 1)
            else:
                rev0 = int(row.get("revision") or 0)
                updated = self._update_remote(core, rev0, str(row.get("kdf_salt") or ""))
                if updated is None:
                    row = self.fetch_remote_row()
                    if row is None:
                        created = self._insert_remote(core); rev = int(created.get("revision") or 1)
                    else:
                        rev0 = int(row.get("revision") or 0)
                        updated = self._update_remote(core, rev0, str(row.get("kdf_salt") or ""))
                        if updated is None:
                            return {"ok": False, "error": "云端刚刚被另一台设备更新，请重新点击一次“保留本机并上传”。"}
                        rev = int(updated.get("revision") or rev0 + 1)
                else:
                    rev = int(updated.get("revision") or rev0 + 1)
            self._accept_synced(core, rev, "已手动用本机版本覆盖云端")
            return {"ok": True, "action": "uploaded", "revision": rev, "asof": now_iso()}

    def resolve(self, strategy: str, load_state: Callable[[], dict[str, Any] | None], save_state: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self.lock:
            m = self._meta(); raw = m.get("pending_remote_core_json"); rev = int(m.get("pending_remote_revision") or 0)
            if not raw or rev <= 0:
                return {"ok": False, "error": "当前没有待处理的同步冲突。"}
            remote_core = json.loads(raw); local_state = load_state() or {}; local_core = state_to_core(local_state)
            row = self.fetch_remote_row()
            if not row:
                return {"ok": False, "error": "云端记录已不存在，请重新同步。"}
            current_rev = int(row.get("revision") or 0)
            if current_rev != rev:
                self._update_meta(pending_remote_core_json=None, pending_remote_revision=None, pending_conflicts_json=None, last_status="云端已变化，请重新同步")
                return {"ok": False, "error": "处理冲突期间云端又发生了变化，请重新同步。"}
            if strategy == "remote":
                new_state = apply_core(local_state, remote_core); save_state(new_state); self._accept_synced(remote_core, current_rev, "已采用云端版本")
                return {"ok": True, "action": "resolved_remote", "state": new_state, "revision": current_rev}
            if strategy == "local":
                updated = self._update_remote(local_core, current_rev, str(row.get("kdf_salt") or ""))
                if updated is None:
                    return {"ok": False, "error": "云端版本已变化，请重新同步。"}
                new_rev = int(updated.get("revision") or current_rev + 1); self._accept_synced(local_core, new_rev, "已采用本机版本")
                return {"ok": True, "action": "resolved_local", "revision": new_rev}
            return {"ok": False, "error": "strategy 必须是 local 或 remote。"}
