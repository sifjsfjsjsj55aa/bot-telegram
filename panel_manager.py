"""
ساخت تست روی پنل از Variables:
PANEL_TYPE = pasarguard | marzban | marzneshin | sanaei
PANEL_BASE_URL, PANEL_USERNAME, PANEL_PASSWORD, PANEL_API_KEY, PANEL_INBOUND_ID
PANEL_TEST_DATA_LIMIT_GB, PANEL_TEST_EXPIRE_HOURS, PANEL_TEST_MESSAGE
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)


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


def _test_limits() -> tuple[int, int, float]:
    hours = int(getattr(config, "PASARGUARD_TEST_EXPIRE_HOURS", 48) or 48)
    gb = float(getattr(config, "PASARGUARD_TEST_DATA_LIMIT_GB", 0.3) or 0.3)
    return int(time.time()) + hours * 3600, hours, gb


def _build_message(username: str, sub: str, location: str = "مولتی") -> str:
    hours = int(getattr(config, "PASARGUARD_TEST_EXPIRE_HOURS", 48) or 48)
    gb = float(getattr(config, "PASARGUARD_TEST_DATA_LIMIT_GB", 0.3) or 0.3)
    if gb >= 1:
        vol = f"{int(gb) if abs(gb - int(gb)) < 1e-9 else gb:g} گیگابایت"
    else:
        vol = f"{int(round(gb * 1024))} مگابایت"
    dur = f"{hours} ساعت" if hours < 24 else f"{hours // 24} روز"
    tpl = getattr(config, "PASARGUARD_TEST_MESSAGE", None) or ""
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


def _base() -> str:
    return (getattr(config, "PANEL_BASE_URL", None) or config.PASARGUARD_BASE_URL or "").rstrip("/")


def _creds() -> tuple[str, str, str]:
    return (
        getattr(config, "PANEL_USERNAME", None) or config.PASARGUARD_USERNAME or "",
        getattr(config, "PANEL_PASSWORD", None) or config.PASARGUARD_PASSWORD or "",
        getattr(config, "PANEL_API_KEY", None) or config.PASARGUARD_API_KEY or "",
    )


async def _marzban_create_test(telegram_user_id: int) -> dict[str, str]:
    base = _base()
    user, password, api_key = _creds()
    expire_ts, hours, gb = _test_limits()
    uname = f"test_{telegram_user_id}_{uuid.uuid4().hex[:6]}"[:32]
    data_limit = int(gb * (1024**3))
    async with httpx.AsyncClient(timeout=40.0, verify=False, follow_redirects=True) as client:
        token = api_key
        if not token:
            r = await client.post(
                f"{base}/api/admin/token",
                data={"username": user, "password": password, "grant_type": "password"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Marzban login failed: {r.status_code} {r.text[:250]}")
            token = r.json().get("access_token") or ""
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "username": uname,
            "proxies": {"vless": {}},
            "inbounds": {},
            "expire": expire_ts,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": f"telegram free test uid={telegram_user_id}",
        }
        r = await client.post(f"{base}/api/user", json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Marzban create failed: {r.status_code} {r.text[:300]}")
        data = r.json()
        final = data.get("username") or uname
        sub = data.get("subscription_url") or ""
        if not sub:
            r2 = await client.get(f"{base}/api/user/{final}", headers=headers)
            if r2.status_code < 400:
                data = r2.json()
                sub = data.get("subscription_url") or ""
        msg = _build_message(final, sub or "", "مرزبان")
        return {"username": final, "subscription_url": sub or "", "message": msg, "expire_at": expire_ts, "kind": "multi"}


async def _marzneshin_create_test(telegram_user_id: int) -> dict[str, str]:
    base = _base()
    user, password, api_key = _creds()
    expire_ts, hours, gb = _test_limits()
    uname = f"test_{telegram_user_id}_{uuid.uuid4().hex[:6]}"[:32]
    data_limit = int(gb * (1024**3))
    async with httpx.AsyncClient(timeout=40.0, verify=False, follow_redirects=True) as client:
        token = api_key
        if not token:
            last = ""
            for path in (f"{base}/api/admins/token", f"{base}/api/admin/token"):
                r = await client.post(
                    path,
                    data={"username": user, "password": password, "grant_type": "password"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                last = f"{r.status_code} {r.text[:200]}"
                if r.status_code < 400:
                    token = r.json().get("access_token") or r.json().get("token") or ""
                    break
            if not token:
                raise RuntimeError(f"Marzneshin login failed: {last}")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        last = ""
        data: dict = {}
        for path, payload in (
            (f"{base}/api/users", {"username": uname, "expire_date": expire_ts, "data_limit": data_limit}),
            (f"{base}/api/user", {"username": uname, "expire": expire_ts, "data_limit": data_limit, "status": "active"}),
        ):
            r = await client.post(path, json=payload, headers=headers)
            last = f"{r.status_code} {r.text[:200]}"
            if r.status_code < 400:
                data = r.json() if r.content else {}
                break
        else:
            raise RuntimeError(f"Marzneshin create failed: {last}")
        final = (data.get("username") if isinstance(data, dict) else None) or uname
        sub = ""
        if isinstance(data, dict):
            sub = data.get("subscription_url") or data.get("sub_link") or ""
        msg = _build_message(final, sub or "", "مرزنشین")
        return {"username": final, "subscription_url": sub or "", "message": msg, "expire_at": expire_ts, "kind": "multi"}


async def _sanaei_create_test(telegram_user_id: int) -> dict[str, str]:
    base = _base()
    user, password, _ = _creds()
    inbound_id = getattr(config, "PANEL_INBOUND_ID", "") or ""
    if not inbound_id:
        raise RuntimeError("PANEL_INBOUND_ID برای سنایی تنظیم نشده.")
    expire_ts, hours, gb = _test_limits()
    expire_ms = expire_ts * 1000
    total_gb_bytes = int(gb * (1024**3))
    email = f"t{telegram_user_id}_{uuid.uuid4().hex[:5]}"
    client_id = str(uuid.uuid4())
    sub_id = uuid.uuid4().hex[:16]

    async with httpx.AsyncClient(timeout=40.0, verify=False, follow_redirects=True) as client:
        r = await client.post(f"{base}/login", data={"username": user, "password": password})
        if r.status_code >= 400:
            r = await client.post(f"{base}/login", json={"username": user, "password": password})
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
            "subId": sub_id,
            "flow": "",
            "comment": f"telegram test {telegram_user_id}",
        }
        payload = {
            "id": int(inbound_id) if str(inbound_id).isdigit() else inbound_id,
            "settings": json.dumps({"clients": [client_obj]}),
        }
        last = ""
        ok = False
        for path in (
            f"{base}/panel/api/inbounds/addClient",
            f"{base}/panel/inbound/addClient",
            f"{base}/xui/inbound/addClient",
        ):
            r2 = await client.post(path, json=payload)
            last = f"{r2.status_code} {r2.text[:200]}"
            if r2.status_code < 400:
                try:
                    j = r2.json()
                    if j.get("success") is False:
                        last = str(j)[:250]
                        continue
                except Exception:
                    pass
                ok = True
                break
        if not ok:
            raise RuntimeError(f"Sanaei addClient failed: {last}")

        sub = f"{base}/sub/{sub_id}"
        msg = _build_message(email, sub, "سنایی")
        return {"username": email, "subscription_url": sub, "message": msg, "expire_at": expire_ts, "kind": "multi"}


async def _pasarguard_create_test(telegram_user_id: int, kind: str = "multi") -> dict[str, str]:
    import panel as pg

    if hasattr(pg, "clear_token_cache"):
        pg.clear_token_cache()
    return await pg.create_test_account(telegram_user_id, kind=kind)


async def create_test_account(telegram_user_id: int, kind: str = "multi") -> dict[str, str]:
    if not config.is_panel_auto_enabled():
        raise RuntimeError(
            "پنل تنظیم نشده. در Variables بگذارید: PANEL_TYPE و PANEL_BASE_URL و "
            "PANEL_USERNAME/PANEL_PASSWORD یا PANEL_API_KEY "
            "(برای سنایی PANEL_INBOUND_ID هم لازم است)."
        )
    ptype = (getattr(config, "PANEL_TYPE", None) or "pasarguard").lower()
    if ptype == "marzban":
        return await _marzban_create_test(telegram_user_id)
    if ptype == "marzneshin":
        return await _marzneshin_create_test(telegram_user_id)
    if ptype == "sanaei":
        return await _sanaei_create_test(telegram_user_id)
    return await _pasarguard_create_test(telegram_user_id, kind=kind)


async def is_panel_ready() -> bool:
    return bool(config.is_panel_auto_enabled())
