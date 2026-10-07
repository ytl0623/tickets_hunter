#!/usr/bin/env python3
#encoding=utf-8
"""
platforms/ctbc.py -- CTBC Sports platform (tix.ctbcsports.com).
中信育樂售票網 (中信特攻 DEA / 中信兄弟 BROTHERS).

Features:
- Member login with OCR captcha (UTK1306)
- VIP / Priority buy login with OCR captcha (UTK0201_01)
- Performance / Date auto select on UTK0201_ with keyword matching & fallback
- Area auto select on UTK0201_001 / UTK0202 / UTK0204 / UTK0205
- Multi-ticket type quantity management & limit enforcement
- Captcha OCR recognition with single-roundtrip verification & fast retry
- Fast reactive addShoppingCart with low-latency server reply polling
- Full checkout automation on UTK0206_ (Cart -> Checkout -> Pickup method -> Payment method -> Credit card info -> Agree terms -> chkNext)
- Real-time notifications (Discord / Telegram) & sound alerts
"""

import asyncio
import base64
import json
import re
import time

from zendriver import cdp

import util
from nodriver_common import (
    CONST_FROM_TOP_TO_BOTTOM,
    CONST_NATIVE_INPUT_SETTER_JS,
    check_and_handle_pause,
    asyncio_sleep_with_pause_check,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
)

__all__ = [
    "CTBC_URL_PATTERNS",
    "is_ctbc_url",
    "get_ctbc_page_type",
    "nodriver_ctbc_extract_captcha_info",
    "nodriver_ctbc_extract_captcha_base64",
    "nodriver_ctbc_dismiss_dialog",
    "nodriver_ctbc_login",
    "nodriver_ctbc_vip_login",
    "nodriver_ctbc_date_auto_select",
    "nodriver_ctbc_area_auto_select",
    "nodriver_ctbc_assign_ticket_number",
    "nodriver_ctbc_captcha_handler",
    "nodriver_ctbc_submit_cart_and_monitor",
    "nodriver_ctbc_checkout",
    "nodriver_ctbc_main",
]

CTBC_URL_PATTERNS = {
    "domain": r"ctbcsports\.com",
    "checkout": r"utk0206",
    "event": r"utk0201",
    "area_computer": r"utk0201_001",
    "area_voucher": r"utk0202",
    "area_table": r"utk0204",
    "seat_map": r"utk0205",
    "login": r"utk130",
}

CONST_CTBC_SUBMIT_COOLDOWN = 10.0

# Module-level state
_state = {
    "checkout_submitted": False,
    "checkout_submitted_time": 0.0,
    "checkout_halted": False,
    "shown_halt_message": False,
    "cart_added": False,
    "played_sound_order": False,
    "shown_checkout_message": False,
    "login_attempted": False,
    "vip_login_attempted": False,
    "last_cart_submit_time": 0.0,
    "last_captcha_src": "",
    "last_captcha_ans": "",
    "last_perf_reload_time": 0.0,
    "sold_out_areas": set(),
}


def is_ctbc_url(url: str) -> bool:
    """Check if the given URL belongs to CTBC Sports ticketing platform."""
    if not url:
        return False
    return "ctbcsports.com" in url.lower()


def get_ctbc_page_type(url: str) -> str:
    """Classify the CTBC page type from URL."""
    if not url:
        return "unknown"
    url_lower = url.lower()

    if "utk0206" in url_lower:
        return "checkout"
    if "utk0205" in url_lower:
        return "seat_map"
    if "utk0201_001" in url_lower:
        return "area_computer"
    if "utk0202" in url_lower:
        return "area_voucher"
    if "utk0204" in url_lower or "utk0203" in url_lower:
        return "area_table"
    if "utk0201" in url_lower:
        return "event"
    if "utk130" in url_lower:
        return "login"
    if (
        "utk0101" in url_lower
        or url_lower.rstrip("/").endswith("ctbcsports.com")
        or ("/dea" in url_lower and not ("utk02" in url_lower or "utk13" in url_lower))
        or ("/brothers" in url_lower and not ("utk02" in url_lower or "utk13" in url_lower))
    ):
        return "home"
    return "other"


async def nodriver_ctbc_extract_captcha_info(
    tab,
    img_selector='#chk_pic, #master_chk_pic, img[src*="pic?TYPE="]',
    input_selector='#CHK, #AMOUNT_CHK, #MASTER_CHK, input[name*="chk"]'
):
    """
    Extract captcha image base64 bytes and current input value in a single CDP roundtrip.
    Renders canvas with solid white background to avoid dark transparent PNG OCR artifacts.
    """
    try:
        script = f'''
            (() => {{
                const input = document.querySelector({json.dumps(input_selector)});
                const currentVal = input ? input.value.trim() : '';

                const img = document.querySelector({json.dumps(img_selector)});
                if (!img || !img.complete || (img.naturalWidth === 0 && img.width === 0)) {{
                    return {{
                        hasImg: false,
                        currentVal: currentVal,
                        dataUrl: null,
                        src: ''
                    }};
                }}

                const w = img.naturalWidth || img.width;
                const h = img.naturalHeight || img.height;
                if (!w || !h) {{
                    return {{
                        hasImg: false,
                        currentVal: currentVal,
                        dataUrl: null,
                        src: img.src || ''
                    }};
                }}

                const canvas = document.createElement('canvas');
                canvas.width = w;
                canvas.height = h;
                const ctx = canvas.getContext('2d');
                ctx.fillStyle = '#FFFFFF';
                ctx.fillRect(0, 0, w, h);
                ctx.drawImage(img, 0, 0, w, h);

                return {{
                    hasImg: true,
                    currentVal: currentVal,
                    dataUrl: canvas.toDataURL('image/png'),
                    src: img.src || ''
                }};
            }})()
        '''
        res = await tab.evaluate(script)
        if isinstance(res, dict):
            current_val = res.get('currentVal', '')
            src = res.get('src', '')
            data_url = res.get('dataUrl')
            img_bytes = None
            if data_url and ',' in data_url:
                img_bytes = base64.b64decode(data_url.split(',', 1)[1])
            return img_bytes, src, current_val
    except Exception:
        pass
    return None, '', ''


async def nodriver_ctbc_extract_captcha_base64(tab, selector='#chk_pic, #master_chk_pic, img[src*="pic?TYPE="]'):
    """Extract captcha image base64 bytes (compatibility wrapper)."""
    img_bytes, src, _ = await nodriver_ctbc_extract_captcha_info(tab, img_selector=selector)
    return img_bytes, src


async def nodriver_ctbc_dismiss_dialog(tab, config_dict=None):
    """Close any blocking jQuery UI popup dialogs and return the alert message."""
    debug = util.create_debug_logger(config_dict) if config_dict else None
    try:
        data = await tab.evaluate('''
            (() => {
                const dialogMsg = document.querySelector('#dialog-message, .ui-dialog-content');
                let text = '';
                if (dialogMsg) {
                    text = dialogMsg.innerText.trim();
                }
                const dialogBtn = document.querySelector(
                    '.ui-dialog-buttonset button, .ui-dialog-buttonpane button, .ui-button, .ui-dialog-titlebar-close'
                );
                if (dialogBtn && dialogBtn.offsetParent !== null) {
                    dialogBtn.click();
                    return { dismissed: true, text: text };
                }
                return { dismissed: false, text: text };
            })()
        ''')
        if isinstance(data, dict) and data.get("dismissed"):
            msg_text = data.get("text", "")
            if debug and msg_text:
                debug.log(f"[CTBC ALERT] Dismissed server popup dialog: '{msg_text}'")
            return msg_text
    except Exception:
        pass
    return None


async def nodriver_ctbc_login(tab, config_dict, ocr):
    """Handle member login for CTBC Sports (via popup dialog or page)."""
    debug = util.create_debug_logger(config_dict)

    # 1. Check if already logged in
    try:
        login_status = await tab.evaluate('''
            (() => {
                const userCell = document.querySelector('.userName');
                if (userCell && (userCell.innerText.includes('您好') || userCell.innerText.includes('登出'))) {
                    return { loggedIn: true, user: userCell.innerText.trim() };
                }
                const logoutLink = document.querySelector('a[onclick*="doLogout"]');
                if (logoutLink) {
                    return { loggedIn: true };
                }
                return { loggedIn: false };
            })()
        ''')
        if isinstance(login_status, dict) and login_status.get("loggedIn"):
            debug.log(f"[CTBC LOGIN] Already logged in as {login_status.get('user', 'member')}")
            return True
    except Exception as exc:
        debug.log(f"[CTBC LOGIN] Error checking login status: {exc}")

    # Check credentials
    account = config_dict.get("accounts", {}).get("ctbc_account", "").strip()
    password = config_dict.get("accounts", {}).get("ctbc_password", "").strip()
    if not account:
        account = config_dict.get("accounts", {}).get("kham_account", "").strip()
        password = config_dict.get("accounts", {}).get("kham_password", "").strip()

    if not account or not password:
        debug.log("[CTBC LOGIN] No CTBC credentials configured, skipping auto-login")
        return False

    # Check if login modal is present
    try:
        modal_visible = await tab.evaluate('''
            (() => {
                const modal = document.querySelector('#popupuser');
                if (!modal) return false;
                const style = window.getComputedStyle(modal);
                return style.display !== 'none' && style.visibility !== 'hidden';
            })()
        ''')

        if not modal_visible:
            # Trigger login modal
            await tab.evaluate('''
                (() => {
                    const modal = document.querySelector('#popupuser');
                    if (modal) {
                        modal.style.display = 'block';
                        return true;
                    }
                    const userBtn = document.querySelector('#userbutton, .user_login, a[href*="UTK130"]');
                    if (userBtn) {
                        userBtn.click();
                        return true;
                    }
                    return false;
                })()
            ''')
            await asyncio_sleep_with_pause_check(0.2, config_dict)

        # Fill account and password using native input setter
        await tab.evaluate(f'''
            (() => {{
                {CONST_NATIVE_INPUT_SETTER_JS}
                const accInput = document.querySelector('#MASTER_ACCOUNT');
                if (accInput) setNativeInputValue(accInput, {json.dumps(account)});
                const pwdInput = document.querySelector('#MASTER_PASSWORD');
                if (pwdInput) setNativeInputValue(pwdInput, {json.dumps(password)});
            }})()
        ''')

        # Captcha OCR
        if ocr and config_dict.get("ocr_captcha", {}).get("enable", True):
            for _ in range(3):
                img_bytes, _, _ = await nodriver_ctbc_extract_captcha_info(
                    tab, img_selector='#master_chk_pic', input_selector='#MASTER_CHK'
                )
                if img_bytes:
                    try:
                        ans = ocr.classification(img_bytes)
                        if ans:
                            ans = re.sub(r'[^a-zA-Z0-9]', '', ans.strip())
                            if len(ans) == 4:
                                debug.log(f"[CTBC LOGIN] OCR answer: {ans}")
                                await tab.evaluate(f'''
                                    (() => {{
                                        {CONST_NATIVE_INPUT_SETTER_JS}
                                        const chk = document.querySelector('#MASTER_CHK');
                                        if (chk) setNativeInputValue(chk, {json.dumps(ans)});
                                    }})()
                                ''')
                                break
                    except Exception as ocr_err:
                        debug.log(f"[CTBC LOGIN] OCR error: {ocr_err}")

                # Refresh captcha
                await tab.evaluate('''
                    (() => {
                        const changPic = document.querySelector('#master_chang_pic');
                        if (changPic) changPic.click();
                    })()
                ''')
                await asyncio_sleep_with_pause_check(0.2, config_dict)

        # Click login button
        await tab.evaluate('''
            (() => {
                if (typeof doMasterLogin === 'function') {
                    doMasterLogin();
                    return true;
                }
                const btn = document.querySelector('#popupuser button.f1, button[onclick*="doMasterLogin"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()
        ''')
        debug.log("[CTBC LOGIN] Submitted login form")
        await asyncio_sleep_with_pause_check(0.3, config_dict)
        return True

    except Exception as exc:
        debug.log(f"[CTBC LOGIN] Login process exception: {exc}")
        return False


async def nodriver_ctbc_vip_login(tab, config_dict, ocr):
    """Handle the VIP / Priority Purchase (#buy) modal on event page."""
    debug = util.create_debug_logger(config_dict)

    # Check if #buy modal is visible
    try:
        is_buy_visible = await tab.evaluate('''
            (() => {
                const buyBox = document.querySelector('#buy');
                if (!buyBox) return false;
                const style = window.getComputedStyle(buyBox);
                return style.display !== 'none' && style.visibility !== 'hidden';
            })()
        ''')
        if not is_buy_visible:
            return False

        debug.log("[CTBC VIP] Priority purchase (#buy) popup detected")

        # Determine values to fill into ID1 & ID2
        id1_val = (
            config_dict.get("accounts", {}).get("ctbc_account", "")
            or config_dict.get("contact", {}).get("real_name", "")
            or config_dict.get("contact", {}).get("phone", "")
            or config_dict.get("user_guess_string", "")
        ).strip()

        id2_val = (
            config_dict.get("accounts", {}).get("ctbc_password", "")
            or config_dict.get("accounts", {}).get("kham_password", "")
        ).strip()

        await tab.evaluate(f'''
            (() => {{
                {CONST_NATIVE_INPUT_SETTER_JS}
                const id1 = document.querySelector('#ID1');
                if (id1 && !id1.value) setNativeInputValue(id1, {json.dumps(id1_val)});
                const tr2 = document.querySelector('#TR2');
                const id2 = document.querySelector('#ID2');
                if (tr2 && window.getComputedStyle(tr2).display !== 'none' && id2 && !id2.value) {{
                    setNativeInputValue(id2, {json.dumps(id2_val)});
                }}
            }})()
        ''')

        # OCR #chk_pic
        if ocr and config_dict.get("ocr_captcha", {}).get("enable", True):
            for _ in range(3):
                img_bytes, _, _ = await nodriver_ctbc_extract_captcha_info(
                    tab, img_selector='#chk_pic', input_selector='#CHK'
                )
                if img_bytes:
                    try:
                        ans = ocr.classification(img_bytes)
                        if ans:
                            ans = re.sub(r'[^a-zA-Z0-9]', '', ans.strip())
                            if len(ans) == 4:
                                debug.log(f"[CTBC VIP] Captcha OCR answer: {ans}")
                                await tab.evaluate(f'''
                                    (() => {{
                                        {CONST_NATIVE_INPUT_SETTER_JS}
                                        const chk = document.querySelector('#CHK');
                                        if (chk) setNativeInputValue(chk, {json.dumps(ans)});
                                    }})()
                                ''')
                                break
                    except Exception as ocr_err:
                        debug.log(f"[CTBC VIP] OCR error: {ocr_err}")

                # Refresh captcha
                await tab.evaluate('''
                    (() => {
                        const changPic = document.querySelector('#chang_pic');
                        if (changPic) changPic.click();
                    })()
                ''')
                await asyncio_sleep_with_pause_check(0.2, config_dict)

        # Click VIP login button
        await tab.evaluate('''
            (() => {
                if (typeof DoVIPLogin === 'function') {
                    DoVIPLogin();
                    return true;
                }
                const btn = document.querySelector('#buy button.f1, button[onclick*="DoVIPLogin"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()
        ''')
        debug.log("[CTBC VIP] DoVIPLogin submitted")
        await asyncio_sleep_with_pause_check(0.3, config_dict)
        return True

    except Exception as exc:
        debug.log(f"[CTBC VIP] VIP login exception: {exc}")
        return False


async def nodriver_ctbc_date_auto_select(tab, config_dict):
    """Auto-select performance date / session on UTK0201_ event page with fast loading and polling."""
    debug = util.create_debug_logger(config_dict)

    if not config_dict.get("date_auto_select", {}).get("enable", True):
        debug.log("[CTBC DATE] Date auto select disabled")
        return False

    date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "").strip()
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    date_auto_fallback = config_dict.get("date_auto_fallback", False)
    date_mode = config_dict.get("date_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)

    # 1. Fast check if Performance list is populated, or trigger ShowPerformance()
    try:
        table_state = await tab.evaluate('''
            (() => {
                const table = document.querySelector('#PerformanceListTable');
                if (!table) return { exists: false, count: 0 };
                const rows = table.querySelectorAll('tr');
                let validCount = 0;
                rows.forEach(r => {
                    if (!r.querySelector('th') && !r.closest('thead')) validCount++;
                });
                return { exists: true, count: validCount };
            })()
        ''')

        if not table_state or table_state.get('count', 0) == 0:
            debug.log("[CTBC DATE] Performance list empty, invoking ShowPerformance()...")
            await tab.evaluate('if (typeof ShowPerformance === "function") ShowPerformance();')
            # Fast poll up to 400ms for rows to appear
            for _ in range(8):
                await asyncio.sleep(0.05)
                cnt = await tab.evaluate('''
                    (() => {
                        const table = document.querySelector('#PerformanceListTable');
                        if (!table) return 0;
                        const rows = table.querySelectorAll('tr:not(thead tr)');
                        return rows.length;
                    })()
                ''')
                if cnt and cnt > 0:
                    break
    except Exception as exc:
        debug.log(f"[CTBC DATE] Check table error: {exc}")

    # 2. Extract performance rows
    try:
        perf_items = await tab.evaluate('''
            (() => {
                const rows = document.querySelectorAll('#PerformanceListTable tr, .ng-star-inserted tr');
                const list = [];
                const soldOutPattern = /已售完|售完|暫無票券|已結束|未開賣/;
                rows.forEach((row, idx) => {
                    if (row.querySelector('th') || row.closest('thead')) return;
                    const text = row.innerText.trim().replace(/\\s+/g, ' ');
                    if (!text || (text.includes('地點') && text.includes('名稱'))) return;

                    const btn = row.querySelector(
                        'button, a.learnmore, .btn, [onclick*="VipSellCheck"], [onclick*="doLink"], [onclick*="UTK"], [onclick*="buy"], input[type="button"]'
                    );
                    const isSoldOut = soldOutPattern.test(text) || row.classList.contains('Soldout') || (btn && btn.disabled);
                    list.push({
                        index: idx,
                        text: text,
                        hasAction: !!btn || text.includes('立即') || text.includes('訂購') || text.includes('預購'),
                        disabled: isSoldOut
                    });
                });
                return list;
            })()
        ''')

        if not perf_items or not isinstance(perf_items, list):
            debug.log("[CTBC DATE] No performance items parsed")
            return False

        # 3. Match candidate
        target_item = None
        keywords = util.parse_keyword_string_to_array(date_keyword) if date_keyword else []

        if keywords:
            for kw in keywords:
                for item in perf_items:
                    if item.get("disabled") or not item.get("hasAction"):
                        continue
                    text = item.get("text", "")
                    if keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, text):
                        continue
                    if util.is_text_match_keyword(kw, text):
                        target_item = item
                        debug.log(f"[CTBC DATE] Matched performance '{text}' with keyword '{kw}'")
                        break
                if target_item:
                    break

        if not target_item:
            if date_auto_fallback:
                available = [
                    item for item in perf_items
                    if not item.get("disabled") and item.get("hasAction") and not (
                        keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, item.get("text", ""))
                    )
                ]
                if available:
                    target_item = util.get_target_item_from_matched_list(available, date_mode)
                    if target_item:
                        debug.log(f"[CTBC DATE] Fallback selected performance: {target_item.get('text')}")
            else:
                # If no session available and auto reload is enabled, refresh ShowPerformance periodically
                auto_reload_interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 0)
                now = time.time()
                if auto_reload_interval > 0:
                    last_reload = _state.get("last_perf_reload_time", 0.0)
                    if (now - last_reload) >= auto_reload_interval:
                        debug.log(f"[CTBC DATE] No target session available. Polling ShowPerformance() (interval={auto_reload_interval}s)...")
                        _state["last_perf_reload_time"] = now
                        await tab.evaluate('if (typeof ShowPerformance === "function") ShowPerformance();')
                return False

        if target_item:
            idx = target_item.get("index", 0)
            debug.log(f"[CTBC DATE] Clicking target performance row index {idx}...")
            await tab.evaluate(f'''
                (() => {{
                    const rows = document.querySelectorAll('#PerformanceListTable tr, .ng-star-inserted tr');
                    const row = rows[{idx}];
                    if (!row) return false;
                    const btn = row.querySelector(
                        'button, a.learnmore, .btn, [onclick*="VipSellCheck"], [onclick*="doLink"], [onclick*="UTK"], [onclick*="buy"], input[type="button"]'
                    );
                    if (btn) {{
                        btn.click();
                        return true;
                    }}
                    row.click();
                    return true;
                }})()
            ''')
            # Short yield so navigation begins without stalling
            await asyncio.sleep(0.05)
            return True

    except Exception as exc:
        debug.log(f"[CTBC DATE] Date select exception: {exc}")
        return False

    return False


async def nodriver_ctbc_area_auto_select(tab, config_dict):
    """
    Auto-select ticket price area with single-roundtrip DOM inspection,
    sold-out caching, and accurate keyword filtering.
    """
    debug = util.create_debug_logger(config_dict)

    if not config_dict.get("area_auto_select", {}).get("enable", True):
        debug.log("[CTBC AREA] Area auto select disabled")
        return {"status": "disabled"}

    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "").strip()
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    area_auto_fallback = config_dict.get("area_auto_fallback", False)
    area_mode = config_dict.get("area_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)

    try:
        # Consolidated inspection of the area page
        page_data = await tab.evaluate('''
            (() => {
                const soldOutPattern = /已售完|售完|0\\s*張|已售罄|暫無票券|目前無票|已無票券/;

                // 1. Check select#PRICE dropdown
                const priceSelect = document.querySelector('select#PRICE, select[id$="_PRICE"]');
                if (priceSelect && priceSelect.options.length > 1) {
                    const selectList = [];
                    for (let i = 0; i < priceSelect.options.length; i++) {
                        const opt = priceSelect.options[i];
                        if (opt.value && opt.value !== '-1') {
                            const isSoldOut = soldOutPattern.test(opt.text) || opt.disabled;
                            selectList.push({
                                index: i,
                                text: opt.text.trim(),
                                value: opt.value,
                                isSelect: true,
                                isCurrent: priceSelect.selectedIndex === i,
                                soldOut: isSoldOut
                            });
                        }
                    }
                    if (selectList.length > 0) {
                        return { type: 'dropdown', items: selectList };
                    }
                }

                // 2. Check table rows or area cards
                const rows = document.querySelectorAll('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item');
                if (rows.length > 0) {
                    const rowList = [];
                    rows.forEach((row, idx) => {
                        if (row.querySelector('th') || row.closest('thead')) return;
                        const text = row.innerText.trim().replace(/\\s+/g, ' ');
                        if (!text || (text.includes('票價') && text.includes('剩餘'))) return;
                        const btn = row.querySelector('button, a, input[type="button"], input[type="submit"]');
                        const isSoldOut = soldOutPattern.test(text)
                            || row.classList.contains('Soldout')
                            || row.classList.contains('soldout')
                            || row.classList.contains('disabled')
                            || (btn && btn.disabled);
                        rowList.push({
                            index: idx,
                            text: text,
                            isSelect: false,
                            soldOut: isSoldOut
                        });
                    });
                    if (rowList.length > 0) {
                        return { type: 'table', items: rowList };
                    }
                }

                // 3. Check direct ticket count page (e.g. UTK0202_ without area list)
                const hasAmountInput = document.querySelector('#AMOUNT, input[KEY="TYPE_ID"], #table_tickettype');
                if (hasAmountInput) {
                    return { type: 'direct', items: [] };
                }

                return { type: 'unknown', items: [] };
            })()
        ''')

        if not isinstance(page_data, dict):
            return {"status": "error"}

        ptype = page_data.get("type")
        if ptype == "direct":
            debug.log("[CTBC AREA] Direct ticket quantity page (no area selection required)")
            return {"status": "ready"}

        area_items = page_data.get("items", [])
        if not area_items:
            debug.log("[CTBC AREA] No area candidates found on page")
            return {"status": "none"}

        # Exclude areas known to be sold out from previous submissions
        sold_out_cache = _state.get("sold_out_areas", set())

        target_area = None
        keywords = util.parse_keyword_string_to_array(area_keyword) if area_keyword else []

        if keywords:
            for kw in keywords:
                for area in area_items:
                    text = area.get("text", "")
                    if area.get("soldOut") or text in sold_out_cache:
                        continue
                    if keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, text):
                        continue
                    if util.is_text_match_keyword(kw, text):
                        target_area = area
                        debug.log(f"[CTBC AREA] Matched area '{text}' with keyword '{kw}'")
                        break
                if target_area:
                    break

        if not target_area:
            if area_auto_fallback:
                available = [
                    a for a in area_items
                    if not a.get("soldOut") and a.get("text") not in sold_out_cache and not (
                        keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, a.get("text", ""))
                    )
                ]
                if available:
                    target_area = util.get_target_item_from_matched_list(available, area_mode)
                    if target_area:
                        debug.log(f"[CTBC AREA] Fallback selected area: {target_area.get('text')}")
            else:
                debug.log("[CTBC AREA] Strict mode: no fallback selected")
                return {"status": "no_match"}

        if target_area:
            idx = target_area.get("index", 0)
            is_sel = target_area.get("isSelect", False)

            if is_sel:
                # Dropdown mode
                if target_area.get("isCurrent"):
                    # Already selected!
                    return {"status": "ready"}

                debug.log(f"[CTBC AREA] Selecting dropdown option index {idx}...")
                await tab.evaluate(f'''
                    (() => {{
                        const priceSelect = document.querySelector('select#PRICE, select[id$="_PRICE"]');
                        if (priceSelect) {{
                            priceSelect.selectedIndex = {idx};
                            priceSelect.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            if (typeof changeArea === 'function') changeArea();
                        }}
                    }})()
                ''')
                await asyncio.sleep(0.05)
                return {"status": "ready"}
            else:
                # Table row / card mode: clicking will navigate to seat/ticket page
                debug.log(f"[CTBC AREA] Clicking table area row index {idx}...")
                await tab.evaluate(f'''
                    (() => {{
                        const rows = document.querySelectorAll('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item');
                        const row = rows[{idx}];
                        if (row) {{
                            const btn = row.querySelector('button, a, input[type="button"], input[type="submit"]');
                            if (btn) btn.click();
                            else row.click();
                        }}
                    }})()
                ''')
                # Must yield immediately so browser navigates to seat map / quantity page!
                await asyncio.sleep(0.05)
                return {"status": "navigated"}

    except Exception as exc:
        debug.log(f"[CTBC AREA] Area selection exception: {exc}")
        return {"status": "error"}

    return {"status": "none"}


async def nodriver_ctbc_assign_ticket_number(tab, config_dict):
    """
    Set desired ticket quantity using native setters.
    Handles single inputs, select dropdowns, and multiple ticket types (filtering excluded and zeroing others).
    """
    requested_qty = int(config_dict.get("ticket_number", 1))
    debug = util.create_debug_logger(config_dict)
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    ticket_type_kw = config_dict.get("ticket_type_keyword", "").strip()

    try:
        qty_info = await tab.evaluate(f'''
            (() => {{
                {CONST_NATIVE_INPUT_SETTER_JS}

                const reqQty = {requested_qty};
                let maxLimit = 99;
                const qLimitEl = document.querySelector('#QUANTITY_LIMIT');
                if (qLimitEl && parseInt(qLimitEl.value) > 0) {{
                    maxLimit = Math.min(maxLimit, parseInt(qLimitEl.value));
                }}
                const firstLimitEl = document.querySelector('#FIRST_QTY_LIMIT');
                if (firstLimitEl && parseInt(firstLimitEl.value) > 0) {{
                    maxLimit = Math.min(maxLimit, parseInt(firstLimitEl.value));
                }}
                const targetQty = Math.max(1, Math.min(reqQty, maxLimit));

                // 1. Check multiple ticket types (UTK0202 / table_tickettype)
                const numInputs = document.querySelectorAll('input.numbox[id="AMOUNT"], input.yd_counterNum, #table_tickettype input[type="number"]');
                if (numInputs.length > 1) {{
                    let targetInput = null;
                    const prefKw = {json.dumps(ticket_type_kw)};
                    const exclKw = {json.dumps(keyword_exclude)};

                    // Look for target input
                    for (let i = 0; i < numInputs.length; i++) {{
                        const inp = numInputs[i];
                        const key = inp.getAttribute('key') || '';
                        const nameEl = key ? document.getElementById(key + '_NAME') : null;
                        const row = inp.closest('tr');
                        const typeName = (nameEl ? nameEl.value : (row ? row.innerText : '')).trim();

                        if (exclKw && typeName.includes(exclKw)) continue;
                        if (prefKw && typeName.includes(prefKw)) {{
                            targetInput = inp;
                            break;
                        }}
                        if (!targetInput) targetInput = inp;
                    }}

                    if (!targetInput && numInputs.length > 0) targetInput = numInputs[0];

                    let changed = false;
                    numInputs.forEach(inp => {{
                        const val = (inp === targetInput) ? targetQty.toString() : '0';
                        if (inp.value !== val) {{
                            setNativeInputValue(inp, val);
                            if (typeof checkNum === 'function') checkNum(inp);
                            changed = true;
                        }}
                    }});

                    return {{
                        assigned: targetQty,
                        limit: maxLimit,
                        multi: true,
                        changed: changed
                    }};
                }}

                // 2. Single AMOUNT input
                const singleInput = document.querySelector('#AMOUNT, input.numbox, input.yd_counterNum, div.qty-select input');
                if (singleInput) {{
                    let changed = false;
                    if (singleInput.value !== targetQty.toString()) {{
                        setNativeInputValue(singleInput, targetQty.toString());
                        singleInput.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                        if (typeof checkNum === 'function') checkNum(singleInput);
                        changed = true;
                    }}
                    return {{
                        assigned: targetQty,
                        limit: maxLimit,
                        multi: false,
                        changed: changed
                    }};
                }}

                // 3. Select dropdown
                const selects = document.querySelectorAll('select#AMOUNT, select.ticket-qty, select[name*="amount"]');
                if (selects.length > 0) {{
                    let changed = false;
                    selects.forEach(sel => {{
                        if (sel.value !== targetQty.toString()) {{
                            sel.value = targetQty.toString();
                            sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            changed = true;
                        }}
                    }});
                    return {{
                        assigned: targetQty,
                        limit: maxLimit,
                        multi: false,
                        changed: changed
                    }};
                }}

                return {{ assigned: 0, limit: maxLimit, changed: false }};
            }})()
        ''')

        if isinstance(qty_info, dict) and qty_info.get("assigned", 0) > 0:
            assigned = qty_info.get("assigned", requested_qty)
            if qty_info.get("changed"):
                debug.log(f"[CTBC COUNT] Ticket quantity set to {assigned} (limit={qty_info.get('limit')})")
            return True
    except Exception as exc:
        debug.log(f"[CTBC COUNT] Set quantity exception: {exc}")
        return False
    return False


async def nodriver_ctbc_captcha_handler(tab, config_dict, ocr):
    """Detect, solve, and fill captcha on purchase/area pages with single-roundtrip verification."""
    debug = util.create_debug_logger(config_dict)

    if not ocr or not config_dict.get("ocr_captcha", {}).get("enable", True):
        return False

    # Extract image base64, src, and current input value in ONE roundtrip
    img_bytes, img_src, current_ans = await nodriver_ctbc_extract_captcha_info(
        tab,
        img_selector='#chk_pic, img[src*="pic?TYPE="]',
        input_selector='#CHK, #AMOUNT_CHK, input[name*="chk"]'
    )

    if not img_bytes:
        return False

    # Check if already solved with valid answer
    if _state.get("last_captcha_src") == img_src and current_ans and len(current_ans) == 4:
        return True

    debug.log("[CTBC CAPTCHA] Found captcha image, running OCR...")
    try:
        ans = ocr.classification(img_bytes)
        if ans:
            ans = re.sub(r'[^a-zA-Z0-9]', '', ans.strip())
            debug.log(f"[CTBC CAPTCHA] OCR result: {ans}")
            if len(ans) == 4:
                _state["last_captcha_src"] = img_src
                _state["last_captcha_ans"] = ans
                await tab.evaluate(f'''
                    (() => {{
                        {CONST_NATIVE_INPUT_SETTER_JS}
                        const input = document.querySelector('#CHK, #AMOUNT_CHK, input[name*="chk"]');
                        if (input) setNativeInputValue(input, {json.dumps(ans)});
                    }})()
                ''')
                return True
            else:
                # Refresh captcha immediately
                debug.log(f"[CTBC CAPTCHA] OCR length mismatch ({len(ans)} != 4), refreshing captcha...")
                _state["last_captcha_src"] = ""
                await tab.evaluate('''
                    (() => {
                        const changPic = document.querySelector('#chang_pic, #master_chang_pic');
                        if (changPic) changPic.click();
                        const input = document.querySelector('#CHK, #AMOUNT_CHK, input[name*="chk"]');
                        if (input) input.value = '';
                    })()
                ''')
                await asyncio.sleep(0.05)
    except Exception as exc:
        debug.log(f"[CTBC CAPTCHA] Exception during OCR: {exc}")

    return False


async def nodriver_ctbc_submit_cart_and_monitor(tab, config_dict):
    """
    Trigger addShoppingCart() and immediately monitor for server response
    (dialog popup or URL redirection) with fast 30ms polling.
    """
    debug = util.create_debug_logger(config_dict)

    # In-flight check
    is_pending = await tab.evaluate('typeof isClick !== "undefined" && isClick === true')
    if is_pending:
        debug.log("[CTBC] addShoppingCart() request is already in-flight, waiting...")
        return "in_flight"

    debug.log("[CTBC] Submitting addShoppingCart()...")
    _state["last_cart_submit_time"] = time.time()

    await tab.evaluate('''
        (() => {
            if (typeof addShoppingCart === 'function') {
                addShoppingCart();
                return true;
            }
            const btn = document.querySelector('button[onclick*="addShoppingCart"], button.red, input[value*="加入購物車"]');
            if (btn) {
                btn.click();
                return true;
            }
            return false;
        })()
    ''')

    # Monitor response immediately for up to 1200ms in fast 30ms checks
    for _ in range(40):
        await asyncio.sleep(0.03)

        # Check URL change
        curr_url = tab.target.url
        if "utk0206" in curr_url.lower():
            debug.log(f"[CTBC] Page transitioned directly to checkout ({curr_url})!")
            _state["cart_added"] = True
            return "success"

        # Check for dialog message
        reply = await tab.evaluate('''
            (() => {
                const dialogMsg = document.querySelector('#dialog-message, .ui-dialog-content');
                if (!dialogMsg) return null;
                const text = dialogMsg.innerText.trim();
                const btn = document.querySelector(
                    '.ui-dialog-buttonset button, .ui-dialog-buttonpane button, .ui-button, .ui-dialog-titlebar-close'
                );
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                }
                return text;
            })()
        ''')

        if reply:
            debug.log(f"[CTBC] Server reply dialog: '{reply}'")
            if "加入購物車完成" in reply or "成功" in reply:
                debug.log("[CTBC] Cart addition successful! Navigating to checkout page...")
                _state["cart_added"] = True
                # Dynamically determine team base path (e.g. /BROTHERS/ or /DEA/)
                await tab.evaluate('''
                    (() => {
                        let basePath = (typeof _vr !== 'undefined' && _vr) ? _vr : '';
                        if (!basePath) {
                            const m = location.pathname.match(/^(\\/[^\\/]+\\/)/);
                            basePath = m ? m[1] : '/DEA/';
                        }
                        location.href = basePath + 'UTK0206_';
                    })()
                ''')
                return "success"

            if "驗證碼" in reply:
                debug.log("[CTBC] Captcha incorrect, refreshing captcha immediately...")
                _state["last_captcha_src"] = ""
                await tab.evaluate('''
                    (() => {
                        const changPic = document.querySelector('#chang_pic, #master_chang_pic');
                        if (changPic) changPic.click();
                        const chk = document.querySelector('#CHK, #AMOUNT_CHK');
                        if (chk) chk.value = '';
                    })()
                ''')
                return "captcha_error"

            if "售完" in reply or "不足" in reply or "無空位" in reply:
                debug.log(f"[CTBC] Area sold out or insufficient seats: '{reply}'")
                return "sold_out"

            return "alert"

    return "in_flight"


async def nodriver_ctbc_checkout(tab, config_dict):
    """
    Fast atomic setup on UTK0206_ checkout page:
    1. Verify cart items & open #paybill (via #Checkout)
    2. Set pickup method (preferred or 128 APP e-ticket)
    3. Set payment method (preferred or 1 Credit Card)
    4. Fill credit card (if 16 digits configured)
    5. Check terms (#agreen)
    6. Ensure sports coins (#M_DONGZI_COUPONS) is UNCHECKED
    7. Halt automation ('之後就不要動') and alert user to manually finish payment
       (or auto-submit if auto_submit_checkout is explicitly enabled)
    """
    debug = util.create_debug_logger(config_dict)

    if _state.get("checkout_halted"):
        return True

    pickup_pref = str(config_dict.get("ctbc", {}).get("pickup_method", "128"))
    pay_pref = str(config_dict.get("ctbc", {}).get("payment_method", "1"))
    card_number = config_dict.get("contact", {}).get("credit_card_prefix", "").strip()

    # Fast consolidated configuration
    res = await tab.evaluate(f'''
        (() => {{
            // 1. Check cart items
            const cartCount = document.querySelector('.cartCount');
            const hasCount = cartCount && parseInt(cartCount.innerText.trim()) > 0;
            const normal = document.querySelector('#normalTicket, .orders');
            const paybill = document.querySelector('#paybill');
            const checkoutBtn = document.querySelector('#Checkout');
            const hasCart = hasCount || !!normal || (paybill && window.getComputedStyle(paybill).display !== 'none') || !!checkoutBtn;
            if (!hasCart) return {{ ready: false, step: 'no_cart' }};

            // 2. Open #paybill if hidden
            let paybillVisible = paybill && window.getComputedStyle(paybill).display !== 'none';
            if (!paybillVisible && checkoutBtn) {{
                checkoutBtn.click();
                paybillVisible = paybill && window.getComputedStyle(paybill).display !== 'none';
            }}

            if (!paybillVisible) {{
                return {{ ready: false, step: 'opening_paybill' }};
            }}

            // 3. Pickup method
            const checkedGet = document.querySelector('input[name="howtoGet"]:checked');
            if (!checkedGet) {{
                const prefGet = document.querySelector('#GetMethods' + {json.dumps(pickup_pref)}) ||
                                document.querySelector('#GetMethods128') ||
                                document.querySelector('input[name="howtoGet"]');
                if (prefGet) prefGet.click();
            }}

            // 4. Payment method
            const checkedPay = document.querySelector('input[name="pay"]:checked');
            if (!checkedPay) {{
                const prefPay = document.querySelector('#PayMethods' + {json.dumps(pay_pref)}) ||
                                document.querySelector('#PayMethods1') ||
                                document.querySelector('input[name="pay"]');
                if (prefPay) prefPay.click();
            }}

            // 5. Fill credit card if 16 digits
            const cardNum = {json.dumps(card_number)};
            if (cardNum && cardNum.length === 16) {{
                const cardInput = document.querySelector('#CARD_NUMBER');
                if (cardInput && !cardInput.value) {{
                    cardInput.value = cardNum;
                    cardInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    cardInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}

            // 6. Agreement checkbox (#agreen) - MUST BE CHECKED
            const agreen = document.querySelector('#agreen');
            if (agreen && !agreen.checked) {{
                agreen.checked = true;
                agreen.dispatchEvent(new Event('input', {{ bubbles: true }}));
                agreen.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}

            // 7. Sports coin (#M_DONGZI_COUPONS) - MUST BE UNCHECKED
            const dongzi = document.querySelector('#M_DONGZI_COUPONS');
            if (dongzi && dongzi.checked) {{
                dongzi.checked = false;
                dongzi.dispatchEvent(new Event('change', {{ bubbles: true }}));
                if (typeof InitDongZi === 'function') InitDongZi();
            }}

            return {{
                ready: true,
                agreenChecked: agreen ? agreen.checked : false,
                dongziChecked: dongzi ? dongzi.checked : false
            }};
        }})()
    ''')

    if not isinstance(res, dict) or not res.get("ready"):
        debug.log(f"[CTBC CHECKOUT] Waiting for paybill form ready (step: {res.get('step') if isinstance(res, dict) else 'unknown'})...")
        return False

    debug.log(f"[CTBC CHECKOUT] Setup completed: agreen={res.get('agreenChecked')}, sports_coins={res.get('dongziChecked')}")

    # Check if user explicitly enabled auto submit
    auto_submit = config_dict.get("ctbc", {}).get("auto_submit_checkout", False)
    if auto_submit:
        now = time.time()
        if _state.get("checkout_submitted"):
            if (now - _state.get("checkout_submitted_time", 0.0)) < CONST_CTBC_SUBMIT_COOLDOWN:
                debug.log("[CTBC CHECKOUT] Checkout submission in cooldown, waiting for response...")
                return True

        debug.log("[CTBC CHECKOUT] Submitting order via chkNext()...")
        submit_result = await tab.evaluate('''
            (() => {
                if (typeof chkNext === 'function') {
                    chkNext();
                    return true;
                }
                const btn = document.querySelector('button[onclick*="chkNext"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()
        ''')
        if submit_result:
            _state["checkout_submitted"] = True
            _state["checkout_submitted_time"] = time.time()
            debug.log("[SUCCESS] CTBC Sports order submitted successfully!")
            if not _state["played_sound_order"]:
                if config_dict.get("advanced", {}).get("play_sound", {}).get("order", True):
                    play_sound_while_ordering(config_dict)
                send_discord_notification(config_dict, "order", "CTBC Sports")
                send_telegram_notification(config_dict, "order", "CTBC Sports")
                _state["played_sound_order"] = True
            return True
    else:
        # Halt automation ('之後就不要動') on checkout page and notify user to complete manually
        _state["checkout_halted"] = True
        debug.log("[SUCCESS] [CTBC CHECKOUT] 結帳頁面約定條款 (#agreen) 已勾選完成，使用運動幣 (#M_DONGZI_COUPONS) 保持不勾選！")
        debug.log("[CTBC CHECKOUT] 依指示停止後續動作（之後就不要動），請手動完成後續付款與確認！")

        if not _state["played_sound_order"]:
            if config_dict.get("advanced", {}).get("play_sound", {}).get("order", True):
                play_sound_while_ordering(config_dict)
            send_discord_notification(config_dict, "order", "CTBC Sports (Checkout Reached)")
            send_telegram_notification(config_dict, "order", "CTBC Sports (Checkout Reached)")
            _state["played_sound_order"] = True

        return True

    return False


async def nodriver_ctbc_main(tab, url, config_dict, ocr):
    """
    Main orchestrator for CTBC Sports platform.
    Dispatches to appropriate handler based on current page URL and DOM state.
    """
    if await check_and_handle_pause(config_dict):
        return tab

    debug = util.create_debug_logger(config_dict)
    page_type = get_ctbc_page_type(url)
    debug.log(f"[CTBC] Current page type: {page_type} (URL: {url})")

    # Reset checkout halted state if navigated away from checkout
    if page_type != "checkout":
        _state["checkout_halted"] = False
        _state["shown_halt_message"] = False
        _state["played_sound_order"] = False

    if page_type in ("checkout", "event"):
        _state["cart_added"] = False
        if page_type == "event":
            _state["sold_out_areas"].clear()

    # 1. Checkout page (UTK0206_)
    if page_type == "checkout":
        if _state.get("checkout_halted"):
            if not _state.get("shown_halt_message"):
                debug.log("[CTBC CHECKOUT] Automation halted on checkout page ('之後就不要動'). Waiting for manual user action.")
                _state["shown_halt_message"] = True
            return tab
        await nodriver_ctbc_checkout(tab, config_dict)
        return tab

    # Dismiss any unhandled alert dialogs and log text (outside checkout page)
    dismissed_alert = await nodriver_ctbc_dismiss_dialog(tab, config_dict)
    if dismissed_alert:
        debug.log(f"[CTBC] Dismissed server alert: '{dismissed_alert}'")
        if "加入購物車完成" in dismissed_alert or "成功" in dismissed_alert:
            debug.log("[CTBC] Cart addition successful! Navigating to checkout page...")
            _state["cart_added"] = True
            await tab.evaluate('''
                (() => {
                    let basePath = (typeof _vr !== 'undefined' && _vr) ? _vr : '';
                    if (!basePath) {
                        const m = location.pathname.match(/^(\\/[^\\/]+\\/)/);
                        basePath = m ? m[1] : '/DEA/';
                    }
                    location.href = basePath + 'UTK0206_';
                })()
            ''')
            return tab

        if "驗證碼" in dismissed_alert:
            _state["last_captcha_src"] = ""
            await tab.evaluate('''
                (() => {
                    const changPic = document.querySelector('#chang_pic, #master_chang_pic');
                    if (changPic) changPic.click();
                    const chk = document.querySelector('#CHK, #AMOUNT_CHK');
                    if (chk) chk.value = '';
                })()
            ''')

    # 2. VIP Priority Purchase modal check (can appear on event or area pages)
    vip_handled = await nodriver_ctbc_vip_login(tab, config_dict, ocr)
    if vip_handled:
        return tab

    # 3. Event / Performance list page (UTK0201_)
    if page_type == "event":
        await nodriver_ctbc_date_auto_select(tab, config_dict)
        return tab

    # 4. Area selection pages (UTK0201_001, UTK0202, UTK0204, UTK0205)
    if page_type in ("area_computer", "area_voucher", "area_table", "seat_map"):
        if _state.get("cart_added"):
            debug.log("[CTBC] Cart addition already completed, waiting for navigation to checkout page (UTK0206_)...")
            return tab

        # Select Area
        area_res = await nodriver_ctbc_area_auto_select(tab, config_dict)
        if area_res.get("status") == "navigated":
            # Clicked a row to navigate into area/seat map; let browser navigate!
            return tab

        # Set ticket quantity
        await nodriver_ctbc_assign_ticket_number(tab, config_dict)

        # Handle Captcha if present
        is_captcha_solved = await nodriver_ctbc_captcha_handler(tab, config_dict, ocr)

        # Add to cart with cooldown
        now = time.time()
        last_submit = _state.get("last_cart_submit_time", 0.0)
        cooldown_ok = (now - last_submit) >= 1.5

        can_submit = is_captcha_solved or not config_dict.get("ocr_captcha", {}).get("enable", True)
        if can_submit and cooldown_ok and not _state.get("cart_added"):
            submit_status = await nodriver_ctbc_submit_cart_and_monitor(tab, config_dict)
            if submit_status == "sold_out":
                # Mark current area as sold out so next tick tries fallback
                curr_area = await tab.evaluate('''
                    (() => {
                        const sel = document.querySelector('select#PRICE, select[id$="_PRICE"]');
                        if (sel && sel.selectedIndex >= 0) return sel.options[sel.selectedIndex].text.trim();
                        return '';
                    })()
                ''')
                if curr_area:
                    _state["sold_out_areas"].add(curr_area)
                    debug.log(f"[CTBC] Added '{curr_area}' to sold out cache")

        return tab

    # 5. Homepage / Login page
    if page_type in ("home", "login"):
        await nodriver_ctbc_login(tab, config_dict, ocr)

        # If user configured a specific event page as homepage, redirect to it
        cfg_homepage = config_dict.get("homepage", "").strip()
        if cfg_homepage and cfg_homepage.lower() != url.lower() and "utk0201" in cfg_homepage.lower():
            debug.log(f"[CTBC] Navigating from home to configured event page: {cfg_homepage}")
            try:
                await tab.get(cfg_homepage)
            except Exception as e:
                debug.log(f"[CTBC] Navigation error: {e}")
        return tab

    return tab
