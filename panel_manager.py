"""
مدیریت چند پنل: pasarguard | marzban | marzneshin | sanaei
ساخت تست رایگان + QR از روی پنل پیش‌فرض ذخیره‌شده در دیتابیس.
اگر پنلی در دیتابیس نباشد، به config پاسارگارد (panel.py) برمی‌گردد.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx

import config
import database as db

logger = logging.getLogger(__name__)

PANEL_TYPES = ("pasarguard", "marzban", "marzneshin", "sanaei")
PANEL_TYPE_LABELS = {
    "pasarguard": "PasarGuard",
    "marzban": "مرزبان (Marzban)",
    "marzneshin": "مرزنشین (Marzneshin)",
    "sanaei": "سنایی / 3x-ui",
}


def make_qr_png(data: str) -> bytes:
    import io
    import qrcode

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _row_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    return {k: row[k] for k in row.keys()}


def _test_limits() -> tuple[int, int, float]:
    """expire_ts, hours, data_gb"""
    hours = int(getattr(config, "PASARGUARD_TEST_EXPIRE_HOURS", 48) or 48)
    gb = float(getattr(config, "PASARGUARD_TEST_DATA_LIMIT_GB", 0.3) or 0.3)
    expire_ts = int(time.time()) + hours * 3600
    return expire_ts, hours, gb


def _build_message(username: str, sub: str, location: str = "مولتی") -> str:
    hours = int(getattr(config, "PASARGUARD_TEST_EXPIRE_HOURS", 48) or 48)
    gb = float(getattr(config, "PASARGUARD_TEST_DATA_LIMIT_GB", 0.3) or 0.3)
    if gb >= 1:
        vol = f"{int(gb) if abs(gb - int(gb)) < 1e-9 else gb:g} گیگابایت"
    else:
        vol = f"{int(round(gb * 1024))} مگابایت"
    dur = f"{hours} ساعت" if hours < 24 else f"{hours // 24} روز"
    tpl = getattr(config, "PASARGUARD_TEST_MESSAGE", None) or (
        "✅ تست با موفقیت آماده شد\n\n"
        "👤 نام کاربری تست : {username}\n"
        "🌐 لوکیشن : {location}\n"
        "⌛ مدت زمان : {duration}\n"
        "📊 حجم تست : {volume}\n\n"
        "لینک اتصال 📎 :\n{subscription_url}"
    )
    try:
        msg = tpl.format(
            username=username,
            location=location,
            duration=dur,
            volume=vol,
            subscription_url=sub or "—",
            service_name=getattr(config, "PASARGUARD_TEST_SERVICE_NAME", "تست"),
        )
    except Exception:
        msg = f"✅ تست آماده شد\n👤 {username}\n🔗 {sub}"
    if sub and sub not in msg:
        msg = msg.rstrip() + f"\n\nلینک اتصال 📎 :\n{sub}"
    return msg


# ---------- Marzban ----------
async def _marzban_token(client: httpx.AsyncClient, base: str, username: str, password: str, api_token: str) -> str:
    if api_token:
        return api_token
    r = await client.post(
        f"{base}/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Marzban login failed: {r.status_code} {r.text[:200]}")
    return r.json().get("access_token") or ""


async def _marzban_create_test(panel: dict, telegram_user_id: int) -> dict[str, str]:
    base = panel["base_url"].rstrip("/")
    expire_ts, hours, gb = _test_limits()
    uname = f"test_{telegram_user_id}_{uuid.uuid4().hex[:6]}"[:32]
    data_limit = int(gb * (1024**3))
    async with httpx.AsyncClient(timeout=40.0, verify=False, follow_redirects=True) as client:
        token = await _marzban_token(
            client, base, panel.get("username") or "", panel.get("password") or "", panel.get("api_token") or ""
        )
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "username": uname,
            "proxies": {"vless": {}, "vmess": {}, "shadowsocks": {}},
            "inbounds": {},
            "expire": expire_ts,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": f"telegram free test uid={telegram_user_id}",
        }
        # minimal proxies if panel requires specific - try vless only
        body["proxies"] = {"vless": {"id": str(uuid.uuid4()), "flow": ""}}
        r = await client.post(f"{base}/api/user", json=body, headers=headers)
        if r.status_code >= 400:
            # try without proxies detail
            body["proxies"] = {"vless": {}}
            r = await client.post(f"{base}/api/user", json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Marzban create user failed: {r.status_code} {r.text[:300]}")
        data = r.json()
        sub = data.get("subscription_url") or ""
        if not sub:
            # fetch user
            r2 = await client.get(f"{base}/api/user/{uname}", headers=headers)
            if r2.status_code < 400:
                data = r2.json()
                sub = data.get("subscription_url") or ""
        if not sub and data.get("username"):
            # token-based sub path common in marzban
            tok = data.get("subscription_url") or data.get("token") or ""
            if isinstance(data.get("links"), list) and data["links"]:
                sub = data["links"][0] if isinstance(data["links"][0], str) else ""
        if not sub:
            # construct from subscription token field
            sub_token = data.get("subscription_url")
            if not sub_token:
                r3 = await client.get(f"{base}/api/user/{uname}", headers=headers)
                if r3.status_code < 400:
                    d3 = r3.json()
                    sub = d3.get("subscription_url") or ""
                    if not sub and d3.get("username"):
                        # Marzban default: {BASE}/sub/{token} — token often in response
                        for k in ("subscription_token", "token", "key"):
                            if d3.get(k):
                                sub = f"{base}/sub/{d3[k]}"
                                break
        final = data.get("username") or uname
        msg = _build_message(final, sub or "", "مرزبان")
        return {"username": final, "subscription_url": sub or "", "message": msg, "expire_at": expire_ts, "kind": "multi"}


# ---------- Marzneshin ----------
async def _marzneshin_create_test(panel: dict, telegram_user_id: int) -> dict[str, str]:
    base = panel["base_url"].rstrip("/")
    expire_ts, hours, gb = _test_limits()
    uname = f"test_{telegram_user_id}_{uuid.uuid4().hex[:6]}"[:32]
    data_limit = int(gb * (1024**3))
    async with httpx.AsyncClient(timeout=40.0, verify=False, follow_redirects=True) as client:
        token = panel.get("api_token") or ""
        if not token:
            r = await client.post(
                f"{base}/api/admins/token",
                data={"username": panel.get("username") or "", "password": panel.get("password") or ""},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code >= 400:
                r = await client.post(
                    f"{base}/api/admin/token",
                    data={"username": panel.get("username") or "", "password": panel.get("password") or "", "grant_type": "password"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            if r.status_code >= 400:
                raise RuntimeError(f"Marzneshin login failed: {r.status_code} {r.text[:200]}")
            token = r.json().get("access_token") or r.json().get("token") or ""
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "username": uname,
            "expire_date": expire_ts,
            "data_limit": data_limit,
            "note": f"telegram free test uid={telegram_user_id}",
        }
        # try several endpoints
        last_err = ""
        data = {}
        for path, payload in (
            (f"{base}/api/users", body),
            (f"{base}/api/user", {**body, "status": "active", "expire": expire_ts}),
        ):
            r = await client.post(path, json=payload, headers=headers)
            if r.status_code < 400:
                data = r.json() if r.content else {}
                break
            last_err = f"{r.status_code} {r.text[:200]}"
        else:
            raise RuntimeError(f"Marzneshin create failed: {last_err}")
        final = (data.get("username") if isinstance(data, dict) else None) or uname
        sub = ""
        if isinstance(data, dict):
            sub = data.get("subscription_url") or data.get("sub_link") or ""
        if not sub:
            r2 = await client.get(f"{base}/api/users/{final}", headers=headers)
            if r2.status_code >= 400:
                r2 = await client.get(f"{base}/api/user/{final}", headers=headers)
            if r2.status_code < 400:
                d2 = r2.json()
                sub = d2.get("subscription_url") or d2.get("sub_link") or ""
        msg = _build_message(final, sub or "", "مرزنشین")
        return {"username": final, "subscription_url": sub or "", "message": msg, "expire_at": expire_ts, "kind": "multi"}


# ---------- Sanaei / 3x-ui ----------
async def _sanaei_create_test(panel: dict, telegram_user_id: int) -> dict[str, str]:
    base = panel["base_url"].rstrip("/")
    expire_ts, hours, gb = _test_limits()
    expire_ms = expire_ts * 1000
    total_gb_bytes = int(gb * (1024**3))
    email = f"t{telegram_user_id}_{uuid.uuid4().hex[:5]}"
    client_id = str(uuid.uuid4())
    inbound_id = panel.get("inbound_id") or ""
    if not inbound_id:
        raise RuntimeError("برای پنل سنایی باید Inbound ID را در ثبت پنل وارد کنید.")

    async with httpx.AsyncClient(timeout=40.0, verify=False, follow_redirects=True) as client:
        # login
        r = await client.post(
            f"{base}/login",
            data={"username": panel.get("username") or "", "password": panel.get("password") or ""},
        )
        if r.status_code >= 400:
            r = await client.post(
                f"{base}/login",
                json={"username": panel.get("username") or "", "password": panel.get("password") or ""},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Sanaei login failed: {r.status_code} {r.text[:200]}")

        client_obj = {
            "id": client_id,
            "email": email,
            "enable": True,
            "expiryTime": expire_ms,
            "totalGB": total_gb_bytes,
            "limitIp": 0,
            "tgId": str(telegram_user_id),
            "subId": uuid.uuid4().hex[:16],
            "flow": "",
            "comment": f"telegram test {telegram_user_id}",
        }
        # 3x-ui style
        payload = {
            "id": int(inbound_id) if str(inbound_id).isdigit() else inbound_id,
            "settings": json.dumps({"clients": [client_obj]}),
        }
        paths = [
            f"{base}/panel/api/inbounds/addClient",
            f"{base}/panel/inbound/addClient",
            f"{base}/xui/inbound/addClient",
        ]
        last = ""
        ok = False
        for path in paths:
            r2 = await client.post(path, json=payload)
            last = f"{r2.status_code} {r2.text[:200]}"
            if r2.status_code < 400:
                try:
                    j = r2.json()
                    if j.get("success") is False:
                        last = str(j)
                        continue
                except Exception:
                    pass
                ok = True
                break
        if not ok:
            raise RuntimeError(f"Sanaei addClient failed: {last}")

        # subscription link: try panel sub path
        sub = ""
        sub_id = client_obj["subId"]
        for path in (
            f"{base}/sub/{sub_id}",
            f"{base}/panel/api/inbounds/getClientTraffics/{email}",
        ):
            if "/sub/" in path:
                sub = path
                break
        # also try get inbound for link template
        msg = _build_message(email, sub or f"{base}/sub/{sub_id}", "سنایی")
        return {
            "username": email,
            "subscription_url": sub or f"{base}/sub/{sub_id}",
            "message": msg,
            "expire_at": expire_ts,
            "kind": "multi",
        }


# ---------- PasarGuard via existing module or DB config ----------
async def _pasarguard_create_test(panel: dict | None, telegram_user_id: int) -> dict[str, str]:
    import panel as pg

    # temporarily override config if DB panel provided
    if panel:
        old = {
            "PASARGUARD_BASE_URL": config.PASARGUARD_BASE_URL,
            "PASARGUARD_USERNAME": config.PASARGUARD_USERNAME,
            "PASARGUARD_PASSWORD": config.PASARGUARD_PASSWORD,
            "PASARGUARD_API_KEY": config.PASARGUARD_API_KEY,
        }
        try:
            config.PASARGUARD_BASE_URL = panel.get("base_url") or config.PASARGUARD_BASE_URL
            config.PASARGUARD_USERNAME = panel.get("username") or ""
            config.PASARGUARD_PASSWORD = panel.get("password") or ""
            config.PASARGUARD_API_KEY = panel.get("api_token") or ""
            if hasattr(pg, "clear_token_cache"):
                pg.clear_token_cache()
            return await pg.create_test_account(telegram_user_id, kind="multi")
        finally:
            for k, v in old.items():
                setattr(config, k, v)
    return await pg.create_test_account(telegram_user_id)


async def create_test_account(telegram_user_id: int, kind: str = "multi") -> dict[str, str]:
    """ساخت تست روی پنل پیش‌فرض (دیتابیس) یا env پاسارگارد."""
    panel_row = await db.get_default_panel()
    panel = _row_dict(panel_row) if panel_row else None

    if not panel:
        # fallback env
        if config.is_panel_auto_enabled():
            import panel as pg

            return await pg.create_test_account(telegram_user_id, kind=kind)
        raise RuntimeError(
            "هیچ پنلی ثبت نشده. از مدیریت ربات → ثبت پنل، یک پنل اضافه کنید "
            "یا PASARGUARD_BASE_URL را در Variables بگذارید."
        )

    ptype = (panel.get("panel_type") or "").lower().strip()
    if ptype == "marzban":
        return await _marzban_create_test(panel, telegram_user_id)
    if ptype == "marzneshin":
        return await _marzneshin_create_test(panel, telegram_user_id)
    if ptype == "sanaei":
        return await _sanaei_create_test(panel, telegram_user_id)
    if ptype == "pasarguard":
        return await _pasarguard_create_test(panel, telegram_user_id)
    raise RuntimeError(f"نوع پنل پشتیبانی نمی‌شود: {ptype}")


async def is_panel_ready() -> bool:
    row = await db.get_default_panel()
    if row:
        return True
    return bool(config.is_panel_auto_enabled())


async def test_panel_connection(panel_id: int) -> str:
    """اتصال را تست می‌کند و پیام وضعیت برمی‌گرداند."""
    row = await db.get_panel(panel_id)
    if not row:
        return "پنل پیدا نشد"
    panel = _row_dict(row)
    ptype = panel.get("panel_type")
    base = (panel.get("base_url") or "").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False, follow_redirects=True) as client:
            if ptype == "sanaei":
                r = await client.post(
                    f"{base}/login",
                    data={"username": panel.get("username") or "", "password": panel.get("password") or ""},
                )
                if r.status_code < 400:
                    return "✅ ورود سنایی موفق"
                return f"❌ ورود سنایی: {r.status_code} {r.text[:120]}"
            if ptype in ("marzban", "pasarguard"):
                r = await client.post(
                    f"{base}/api/admin/token",
                    data={
                        "username": panel.get("username") or "",
                        "password": panel.get("password") or "",
                        "grant_type": "password",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if r.status_code < 400 or panel.get("api_token"):
                    return f"✅ اتصال {ptype} برقرار است"
                return f"❌ لاگین: {r.status_code} {r.text[:120]}"
            if ptype == "marzneshin":
                r = await client.get(base)
                return f"✅ سرور در دسترس ({r.status_code})" if r.status_code < 500 else f"❌ {r.status_code}"
        return "نوع ناشناخته"
    except Exception as e:
        return f"❌ خطا: {e}"
