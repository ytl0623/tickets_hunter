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
    "nodriver_ctbc_home_auto_select",
    "nodriver_ctbc_date_auto_select",
    "nodriver_ctbc_area_auto_select",
    "nodriver_ctbc_seat_auto_select",
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
    "last_seat_selection_time": 0.0,
    "last_home_click_time": 0.0,
    "last_date_click_time": 0.0,
    "last_area_click_time": 0.0,
    "last_page_type": "",
    "last_logged_url": "",
    "navigating_back": False,
    "no_creds_logged": False,
    "logged_in_logged": False,
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
        or "utk0102" in url_lower
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
            if not _state.get("logged_in_logged"):
                debug.log(f"[CTBC LOGIN] Already logged in as {login_status.get('user', 'member')}")
                _state["logged_in_logged"] = True
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
        if not _state.get("no_creds_logged"):
            debug.log("[CTBC LOGIN] No CTBC credentials configured, skipping auto-login")
            _state["no_creds_logged"] = True
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


async def nodriver_ctbc_home_auto_select(tab, config_dict):
    """
    Auto-select match on CTBC homepage (UTK0101_) or game list (UTK0102_).
    Matches against date_keyword or game_name, or clicks first available match.
    """
    debug = util.create_debug_logger(config_dict)

    now = time.time()
    if (now - _state.get("last_home_click_time", 0.0)) < 1.2:
        return False

    date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "").strip()
    game_name = config_dict.get("game_name", "").strip()
    target_kw = date_keyword or game_name
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    date_auto_fallback = config_dict.get("date_auto_fallback", False)

    try:
        page_info = await tab.evaluate('''
            (() => {
                const links = document.querySelectorAll(
                    '.container a[href*="UTK0201_"], #calendar a[href*="UTK0201_"], .royalSlider a[href*="UTK0201_"], #promoteEvent a[href*="UTK0201_"], a[href*="UTK0201_"]'
                );
                const items = [];
                links.forEach((a, idx) => {
                    const text = a.innerText.trim().replace(/\\s+/g, ' ');
                    const href = a.getAttribute('href') || a.href || '';
                    if (href && href.indexOf('UTK0201_') >= 0) {
                        items.push({ index: idx, text: text, href: href });
                    }
                });
                const hasMenuList = !!document.querySelector('#menu_category a[href*="UTK0102_"]');
                return { items: items, hasMenuList: hasMenuList };
            })()
        ''')

        if not isinstance(page_info, dict):
            return False

        items = page_info.get("items", [])
        keywords = util.parse_keyword_string_to_array(target_kw) if target_kw else []

        target_item = None
        if keywords:
            for kw in keywords:
                for it in items:
                    t = it.get("text", "")
                    h = it.get("href", "")
                    if keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, t):
                        continue
                    if util.is_text_match_keyword(kw, t) or util.is_text_match_keyword(kw, h):
                        target_item = it
                        debug.log(f"[CTBC HOME] Matched match '{t}' with keyword '{kw}'")
                        break
                if target_item:
                    break

        if not target_item:
            if date_auto_fallback and items:
                target_item = items[0]
                debug.log(f"[CTBC HOME] Fallback selected first match: '{target_item.get('text')}'")
            elif page_info.get("hasMenuList") and not items:
                _state["last_home_click_time"] = time.time()
                debug.log("[CTBC HOME] Navigating to match list (UTK0102_)...")
                await tab.evaluate('''
                    (() => {
                        const menuBtn = document.querySelector('#menu_category a[href*="UTK0102_"]');
                        if (menuBtn) {
                            menuBtn.click();
                        } else {
                            let basePath = (typeof _vr !== 'undefined' && _vr) ? _vr : '';
                            if (!basePath) {
                                const m = location.pathname.match(/^(\\/[^\\/]+\\/)/);
                                basePath = m ? m[1] : '/BROTHERS/';
                            }
                            location.href = basePath + 'UTK0102_?TYPE=4';
                        }
                    })()
                ''')
                await asyncio.sleep(0.1)
                return True

        if target_item:
            _state["last_home_click_time"] = time.time()
            idx = target_item.get("index", 0)
            href = target_item.get("href", "")
            debug.log(f"[CTBC HOME] Clicking match link index {idx} ({href})...")
            await tab.evaluate(f'''
                (() => {{
                    const links = document.querySelectorAll(
                        '.container a[href*="UTK0201_"], #calendar a[href*="UTK0201_"], .royalSlider a[href*="UTK0201_"], #promoteEvent a[href*="UTK0201_"], a[href*="UTK0201_"]'
                    );
                    const a = links[{idx}];
                    if (a) {{
                        a.click();
                        return true;
                    }}
                    if ({json.dumps(href)}) {{
                        location.href = {json.dumps(href)};
                        return true;
                    }}
                    return false;
                }})()
            ''')
            await asyncio.sleep(0.1)
            return True

    except Exception as exc:
        debug.log(f"[CTBC HOME] Auto select match error: {exc}")

    return False


async def nodriver_ctbc_date_auto_select(tab, config_dict):
    """Auto-select performance date / session on UTK0201_ event page with fast loading and polling."""
    debug = util.create_debug_logger(config_dict)

    if not config_dict.get("date_auto_select", {}).get("enable", True):
        debug.log("[CTBC DATE] Date auto select disabled")
        return False

    now = time.time()
    if (now - _state.get("last_date_click_time", 0.0)) < 3.0:
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
            _state["last_date_click_time"] = time.time()
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
                        const href = btn.getAttribute('href') || '';
                        if (href.toLowerCase().startsWith('javascript:')) {{
                            try {{
                                eval(href.replace(/^javascript:/i, '').trim());
                                return true;
                            }} catch (e) {{}}
                        }}
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
    supporting table rows (tr.saleTr on UTK0204_), dropdowns, and cards.
    Filters out sold-out areas and matches area name or ticket price.
    """
    debug = util.create_debug_logger(config_dict)

    if not config_dict.get("area_auto_select", {}).get("enable", True):
        debug.log("[CTBC AREA] Area auto select disabled")
        return {"status": "disabled"}

    now = time.time()
    if (now - _state.get("last_area_click_time", 0.0)) < 3.5:
        return {"status": "cooldown"}

    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "").strip()
    price_keyword = (
        config_dict.get("area_auto_select", {}).get("price_keyword", "")
        or config_dict.get("ctbc", {}).get("price_keyword", "")
    ).strip()
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    area_auto_fallback = config_dict.get("area_auto_fallback", False)
    area_mode = config_dict.get("area_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)

    try:
        # Consolidated inspection of the area page
        page_data = await tab.evaluate('''
            (() => {
                const soldOutPattern = /已售完|售完|0\\s*張|已售罄|暫無票券|目前無票|已無票券|已結束/;

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
                                isSaleTr: false,
                                isCurrent: priceSelect.selectedIndex === i,
                                soldOut: isSoldOut
                            });
                        }
                    }
                    if (selectList.length > 0) {
                        return { type: 'dropdown', items: selectList };
                    }
                }

                // 2. Check tr.saleTr (UTK0204_ / UTK0203_)
                const saleRows = document.querySelectorAll('tr.saleTr, table.f1 tr.saleTr');
                if (saleRows.length > 0) {
                    const rowList = [];
                    saleRows.forEach((row, idx) => {
                        const areaTd = row.querySelector('[data-title*="票區"]') || (row.cells && row.cells.length > 1 ? row.cells[1] : null);
                        const priceTd = row.querySelector('[data-title*="票價"]') || (row.cells && row.cells.length > 2 ? row.cells[2] : null);
                        const seatTd = row.querySelector('[data-title*="空位"]') || (row.cells && row.cells.length > 3 ? row.cells[3] : null);

                        const areaName = areaTd ? areaTd.innerText.trim() : '';
                        const price = priceTd ? priceTd.innerText.trim() : '';
                        const seatStatus = seatTd ? seatTd.innerText.trim() : '';
                        const rel = row.getAttribute('rel') || '';

                        if (!areaName && !price) return;

                        const isSoldOut = soldOutPattern.test(seatStatus)
                            || seatStatus === '0'
                            || row.classList.contains('Soldout')
                            || row.classList.contains('soldout')
                            || row.classList.contains('disabled');

                        rowList.push({
                            index: idx,
                            areaName: areaName,
                            price: price,
                            seatStatus: seatStatus,
                            text: `${areaName} ${price} ${seatStatus}`.trim(),
                            rel: rel,
                            isSelect: false,
                            isSaleTr: true,
                            soldOut: isSoldOut
                        });
                    });
                    if (rowList.length > 0) {
                        return { type: 'sale_tr', items: rowList };
                    }
                }

                // 3. Check generic table rows or area cards
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
                            isSaleTr: false,
                            soldOut: isSoldOut
                        });
                    });
                    if (rowList.length > 0) {
                        return { type: 'table', items: rowList };
                    }
                }

                // 4. Check direct ticket count page (e.g. UTK0202_ without area list)
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
            return {"status": "none"}

        # Exclude areas known to be sold out from previous submissions
        sold_out_cache = _state.get("sold_out_areas", set())

        target_area = None
        target_kw = area_keyword or price_keyword
        keywords = util.parse_keyword_string_to_array(target_kw) if target_kw else []

        if keywords:
            for kw in keywords:
                for area in area_items:
                    text = area.get("text", "")
                    area_name = area.get("areaName", "")
                    if area.get("soldOut") or text in sold_out_cache or (area_name and area_name in sold_out_cache):
                        continue
                    if keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, text):
                        continue
                    if util.is_text_match_keyword(kw, text) or (area_name and util.is_text_match_keyword(kw, area_name)):
                        target_area = area
                        debug.log(f"[CTBC AREA] Matched area '{text}' with keyword '{kw}'")
                        break
                if target_area:
                    break

        if not target_area:
            if area_auto_fallback:
                available = [
                    a for a in area_items
                    if not a.get("soldOut")
                    and a.get("text") not in sold_out_cache
                    and (not a.get("areaName") or a.get("areaName") not in sold_out_cache)
                    and not (
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
            is_sale_tr = target_area.get("isSaleTr", False)
            rel = target_area.get("rel", "")

            if is_sel:
                # Dropdown mode
                if target_area.get("isCurrent"):
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
            elif is_sale_tr:
                _state["last_area_click_time"] = time.time()
                debug.log(f"[CTBC AREA] Clicking table area row index {idx} (rel='{rel}', text='{target_area.get('text')}')...")
                await tab.evaluate(f'''
                    (() => {{
                        const saleRows = document.querySelectorAll('tr.saleTr, table.f1 tr.saleTr');
                        const row = saleRows[{idx}];
                        if (row) {{
                            const rel = row.getAttribute('rel') || {json.dumps(rel)};
                            if (rel) {{
                                const mapArea = document.getElementById(rel) || document.querySelector(`area#${{rel}}`);
                                if (mapArea) {{
                                    const aHref = mapArea.getAttribute('href') || '';
                                    if (aHref.toLowerCase().startsWith('javascript:')) {{
                                        try {{
                                            eval(aHref.replace(/^javascript:/i, '').trim());
                                            return true;
                                        }} catch (e) {{}}
                                    }}
                                    mapArea.click();
                                }}
                            }}
                            const aTag = row.querySelector('a');
                            if (aTag) {{
                                const aHref = aTag.getAttribute('href') || '';
                                if (aHref.toLowerCase().startsWith('javascript:')) {{
                                    try {{
                                        eval(aHref.replace(/^javascript:/i, '').trim());
                                        return true;
                                    }} catch (e) {{}}
                                }}
                            }}
                            row.click();
                            return true;
                        }}
                        return false;
                    }})()
                ''')
                await asyncio.sleep(0.05)
                return {"status": "navigated"}
            else:
                # Table row / card mode: clicking will navigate to seat/ticket page
                _state["last_area_click_time"] = time.time()
                debug.log(f"[CTBC AREA] Clicking table area row index {idx}...")
                await tab.evaluate(f'''
                    (() => {{
                        const rows = document.querySelectorAll('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item');
                        const row = rows[{idx}];
                        if (row) {{
                            const btn = row.querySelector('button, a, input[type="button"], input[type="submit"]');
                            if (btn) {{
                                const href = btn.getAttribute('href') || '';
                                if (href.toLowerCase().startsWith('javascript:')) {{
                                    try {{
                                        eval(href.replace(/^javascript:/i, '').trim());
                                        return true;
                                    }} catch (e) {{}}
                                }}
                                btn.click();
                            }} else {{
                                row.click();
                            }}
                        }}
                    }})()
                ''')
                await asyncio.sleep(0.05)
                return {"status": "navigated"}

    except Exception as exc:
        debug.log(f"[CTBC AREA] Area selection exception: {exc}")
        return {"status": "error"}

    return {"status": "none"}


async def nodriver_ctbc_seat_auto_select(tab, config_dict):
    """
    Auto-select ticket type and pick seats on CTBC seat map page (UTK0205_).
    1. Select ticket type (prefer 全票 or ticket_type_keyword, avoid disability unless configured).
    2. Pick contiguous/available empty seats in #TBL up to ticket_number limit.
    """
    debug = util.create_debug_logger(config_dict)
    ticket_type_kw = (
        config_dict.get("ctbc", {}).get("ticket_type_keyword", "")
        or config_dict.get("ticket_type_keyword", "")
    ).strip()
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    requested_qty = int(config_dict.get("ticket_number", 1))

    try:
        # 1. Inspect seat map and ticket buttons in single roundtrip
        page_info = await tab.evaluate('''
            (() => {
                const tbl = document.querySelector('#TBL');
                if (!tbl || typeof seats === 'undefined' || Object.keys(seats).length === 0) {
                    return { status: 'loading' };
                }

                const ticketBtns = document.querySelectorAll('.ticket button:not(#tdtextlocation)');
                if (!ticketBtns || ticketBtns.length === 0) {
                    return { status: 'loading' };
                }

                const typeList = [];
                ticketBtns.forEach((btn, idx) => {
                    const numDiv = btn.querySelector('div.checkNum');
                    const typeId = numDiv ? (numDiv.id || numDiv.getAttribute('id')) : '';
                    const typeVal = numDiv ? (numDiv.getAttribute('val') || '') : '';
                    const text = btn.innerText.trim().replace(/\\s+/g, ' ');
                    const count = numDiv ? parseInt(numDiv.innerText.trim() || '0', 10) : 0;
                    const color = numDiv ? (numDiv.getAttribute('color') || '') : '';
                    typeList.push({
                        index: idx,
                        id: typeId,
                        val: typeVal,
                        text: text,
                        count: isNaN(count) ? 0 : count,
                        color: color
                    });
                });

                let totalSelected = 0;
                for (const k in seats) {
                    if (seats[k].A === 'S') totalSelected++;
                }

                let maxLimit = 99;
                const qLimitEl = document.querySelector('#QUANTITY_LIMIT');
                if (qLimitEl && parseInt(qLimitEl.value) > 0) {
                    maxLimit = Math.min(maxLimit, parseInt(qLimitEl.value));
                }
                const firstLimitEl = document.querySelector('#FIRST_QTY_LIMIT');
                if (firstLimitEl && parseInt(firstLimitEl.value) > 0) {
                    maxLimit = Math.min(maxLimit, parseInt(firstLimitEl.value));
                }

                let emptyCount = 0;
                for (const k in seats) {
                    if (seats[k].S == 0 && (!seats[k].A || seats[k].A === '')) {
                        emptyCount++;
                    }
                }

                const curType = (typeof currType !== 'undefined' && currType) ? currType : '';
                const curTypeName = (typeof currTypeName !== 'undefined' && currTypeName) ? currTypeName : '';

                return {
                    status: 'ready',
                    types: typeList,
                    totalSelected: totalSelected,
                    maxLimit: maxLimit,
                    emptyCount: emptyCount,
                    currType: curType,
                    currTypeName: curTypeName
                };
            })()
        ''')

        if not isinstance(page_info, dict) or page_info.get("status") == "loading":
            debug.log("[CTBC SEAT] Seat map or ticket types still loading...")
            return {"status": "loading"}

        total_selected = page_info.get("totalSelected", 0)
        max_limit = page_info.get("maxLimit", 99)
        target_qty = max(1, min(requested_qty, max_limit))
        empty_count = page_info.get("emptyCount", 0)
        types = page_info.get("types", [])

        # Check if already fulfilled
        if total_selected >= target_qty:
            debug.log(f"[CTBC SEAT] Target quantity fulfilled ({total_selected}/{target_qty} selected)")
            return {"status": "ready", "selected": total_selected, "target": target_qty}

        # Check if area is completely sold out
        if empty_count == 0 and total_selected == 0:
            debug.log("[CTBC SEAT] No empty seats available in this area!")
            return {"status": "sold_out"}

        # 2. Select ticket type
        target_type = None
        concession_pattern = re.compile(r'身心障礙|輪椅|愛心|陪伴|半票|學生|優待')

        if ticket_type_kw:
            keywords = util.parse_keyword_string_to_array(ticket_type_kw)
            for kw in keywords:
                for t in types:
                    text = t.get("text", "")
                    val = t.get("val", "")
                    if keyword_exclude and (
                        util.is_text_match_keyword(keyword_exclude, text)
                        or util.is_text_match_keyword(keyword_exclude, val)
                    ):
                        continue
                    if util.is_text_match_keyword(kw, text) or util.is_text_match_keyword(kw, val):
                        target_type = t
                        debug.log(f"[CTBC SEAT] Matched ticket type '{text}' with keyword '{kw}'")
                        break
                if target_type:
                    break

        if not target_type:
            # Default: prefer "全票", avoid concession/disability
            for t in types:
                text = t.get("text", "")
                val = t.get("val", "")
                if keyword_exclude and (
                    util.is_text_match_keyword(keyword_exclude, text)
                    or util.is_text_match_keyword(keyword_exclude, val)
                ):
                    continue
                if concession_pattern.search(text) or concession_pattern.search(val):
                    continue
                if "全票" in text or "全票" in val:
                    target_type = t
                    debug.log(f"[CTBC SEAT] Default selected ticket type: '{text}'")
                    break

        if not target_type:
            # Fallback to first non-concession type, or first type
            non_concession = [
                t for t in types
                if not concession_pattern.search(t.get("text", ""))
                and not concession_pattern.search(t.get("val", ""))
            ]
            target_type = non_concession[0] if non_concession else types[0]
            debug.log(f"[CTBC SEAT] Fallback ticket type: '{target_type.get('text')}'")

        needed = target_qty - total_selected
        target_id = target_type.get("id", "")
        target_val = target_type.get("val", "")
        target_idx = target_type.get("index", 0)

        debug.log(f"[CTBC SEAT] Selecting ticket type '{target_val}' (id={target_id}) and picking {needed} seats...")

        # 3. Pick seats in DOM
        pick_res = await tab.evaluate(f'''
            (() => {{
                const targetId = {json.dumps(target_id)};
                const targetVal = {json.dumps(target_val)};
                const targetIdx = {target_idx};
                const needed = {needed};

                // Set ticket type
                if (typeof setType === 'function') {{
                    setType(targetId, targetVal);
                }}
                const btns = document.querySelectorAll('.ticket button:not(#tdtextlocation)');
                if (btns && btns[targetIdx]) {{
                    btns[targetIdx].click();
                }}

                const table = document.querySelector('#TBL');
                if (!table || typeof seats === 'undefined') return {{ picked: 0, finalSelected: 0 }};

                const candidates = [];
                for (const k in seats) {{
                    const s = seats[k];
                    if (s.S == 0 && (!s.A || s.A === '')) {{
                        const x = parseInt(k.substring(1, 3), 10);
                        const y = parseInt(k.substring(3, 5), 10);
                        const name = s.I || '';
                        const rowMatch = name.match(/-(\\d+)排/);
                        const seatMatch = name.match(/-(\\d+)號/);
                        const rowNum = rowMatch ? parseInt(rowMatch[1], 10) : y;
                        const seatNum = seatMatch ? parseInt(seatMatch[1], 10) : x;

                        candidates.push({{
                            key: k,
                            name: name,
                            x: x,
                            y: y,
                            rowNum: rowNum,
                            seatNum: seatNum
                        }});
                    }}
                }}

                if (candidates.length === 0) return {{ picked: 0, finalSelected: 0 }};

                // Group by row to find contiguous seats
                const rowMap = {{}};
                candidates.forEach(c => {{
                    if (!rowMap[c.rowNum]) rowMap[c.rowNum] = [];
                    rowMap[c.rowNum].push(c);
                }});

                let selectedList = [];

                // Try to find contiguous seats in the same row
                for (const r in rowMap) {{
                    const rowSeats = rowMap[r];
                    rowSeats.sort((a, b) => a.seatNum - b.seatNum);

                    for (let i = 0; i <= rowSeats.length - needed; i++) {{
                        let isContiguous = true;
                        for (let j = 0; j < needed - 1; j++) {{
                            const diff = rowSeats[i + j + 1].seatNum - rowSeats[i + j].seatNum;
                            if (diff !== 1 && diff !== 2) {{
                                isContiguous = false;
                                break;
                            }}
                        }}
                        if (isContiguous) {{
                            selectedList = rowSeats.slice(i, i + needed);
                            break;
                        }}
                    }}
                    if (selectedList.length === needed) break;
                }}

                // Fallback: pick any available seats in the same row or any row
                if (selectedList.length < needed) {{
                    candidates.sort((a, b) => {{
                        if (a.rowNum !== b.rowNum) return a.rowNum - b.rowNum;
                        return a.seatNum - b.seatNum;
                    }});
                    selectedList = candidates.slice(0, needed);
                }}

                // Click each cell
                let pickedCount = 0;
                selectedList.forEach(item => {{
                    let cell = null;
                    if (table.rows[item.y] && table.rows[item.y].cells[item.x]) {{
                        cell = table.rows[item.y].cells[item.x];
                    }}
                    if (!cell && item.name) {{
                        cell = document.querySelector(`#TBL td[title="${{CSS.escape(item.name)}}"]`);
                    }}
                    if (cell) {{
                        if (typeof $ !== 'undefined') {{
                            $(cell).trigger('click');
                        }} else {{
                            cell.click();
                        }}
                        pickedCount++;
                    }}
                }});

                // Dismiss checkPi row-change alert dialog if triggered
                const dialogBtn = document.querySelector('.ui-dialog-buttonset button, .ui-dialog-buttonpane button, .ui-button');
                if (dialogBtn && dialogBtn.offsetParent !== null) {{
                    dialogBtn.click();
                }}

                let finalSelected = 0;
                for (const k in seats) {{
                    if (seats[k].A === 'S') finalSelected++;
                }}

                return {{
                    picked: pickedCount,
                    finalSelected: finalSelected,
                    pickedNames: selectedList.map(s => s.name)
                }};
            }})()
        ''')

        if isinstance(pick_res, dict):
            final_sel = pick_res.get("finalSelected", 0)
            picked_names = pick_res.get("pickedNames", [])
            debug.log(f"[CTBC SEAT] Picked {pick_res.get('picked')} seats: {picked_names} (total selected: {final_sel}/{target_qty})")
            if final_sel >= target_qty:
                return {"status": "ready", "selected": final_sel, "target": target_qty}
            elif final_sel > 0:
                return {"status": "partial", "selected": final_sel, "target": target_qty}

    except Exception as exc:
        debug.log(f"[CTBC SEAT] Seat selection exception: {exc}")

    return {"status": "error"}


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
    last_page_type = _state.get("last_page_type", "")
    if page_type != last_page_type:
        _state["last_page_type"] = page_type
        _state["navigating_back"] = False
        if page_type == "area_table":
            _state["last_area_click_time"] = 0.0
        elif page_type == "event":
            _state["last_date_click_time"] = 0.0
        elif page_type == "home":
            _state["last_home_click_time"] = 0.0

    last_logged_url = _state.get("last_logged_url", "")
    if url != last_logged_url:
        _state["last_logged_url"] = url
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
            await asyncio.sleep(0.5)
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
                        basePath = m ? m[1] : '/BROTHERS/';
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

    # 4. Area selection table pages (UTK0204_, UTK0203_)
    if page_type == "area_table":
        if _state.get("cart_added"):
            debug.log("[CTBC] Cart addition already completed, waiting for navigation to checkout page (UTK0206_)...")
            return tab

        area_res = await nodriver_ctbc_area_auto_select(tab, config_dict)
        if area_res.get("status") == "navigated":
            return tab
        if area_res.get("status") == "no_match":
            auto_reload_interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 0)
            now = time.time()
            if auto_reload_interval > 0:
                last_reload = _state.get("last_perf_reload_time", 0.0)
                if (now - last_reload) >= auto_reload_interval:
                    debug.log(f"[CTBC AREA] No available target area. Reloading area page (interval={auto_reload_interval}s)...")
                    _state["last_perf_reload_time"] = now
                    try:
                        await tab.reload()
                    except Exception as e:
                        debug.log(f"[CTBC AREA] Reload error: {e}")
        return tab

    # 5. Seat map page (UTK0205_)
    if page_type == "seat_map":
        if _state.get("cart_added"):
            debug.log("[CTBC] Cart addition already completed, waiting for navigation to checkout page (UTK0206_)...")
            return tab

        # Step 1 & 2: Select ticket type and pick seats
        seat_res = await nodriver_ctbc_seat_auto_select(tab, config_dict)
        status = seat_res.get("status")

        if status == "sold_out":
            if _state.get("navigating_back"):
                debug.log("[CTBC SEAT] Already navigating back to area table...")
                return tab

            _state["navigating_back"] = True
            # Current area has no available seats! Add to sold out cache and return to area table
            curr_area = await tab.evaluate('''
                (() => {
                    const el = document.querySelector('#PRICE_AREA_NAME, .area');
                    return el ? (el.value || el.innerText || '').trim() : '';
                })()
            ''')
            if curr_area:
                _state["sold_out_areas"].add(curr_area)
                debug.log(f"[CTBC SEAT] Area '{curr_area}' has no available seats! Added to sold out cache, navigating back...")
            else:
                debug.log("[CTBC SEAT] Current area has no available seats! Navigating back...")

            await tab.evaluate('''
                (() => {
                    if (typeof backUrl !== 'undefined' && backUrl) {
                        location.href = backUrl;
                    } else {
                        top.history.go(-1);
                    }
                })()
            ''')
            await asyncio.sleep(0.1)
            return tab

        if status == "loading":
            return tab

        selected_count = seat_res.get("selected", 0)

        # Step 3: Handle Captcha if present
        is_captcha_solved = await nodriver_ctbc_captcha_handler(tab, config_dict, ocr)

        # Step 4: Add to cart only when seats are selected!
        now = time.time()
        last_submit = _state.get("last_cart_submit_time", 0.0)
        cooldown_ok = (now - last_submit) >= 1.5

        can_submit = (selected_count > 0) and (
            is_captcha_solved or not config_dict.get("ocr_captcha", {}).get("enable", True)
        )

        if can_submit and cooldown_ok and not _state.get("cart_added"):
            submit_status = await nodriver_ctbc_submit_cart_and_monitor(tab, config_dict)
            if submit_status == "sold_out":
                _state["navigating_back"] = True
                curr_area = await tab.evaluate('''
                    (() => {
                        const el = document.querySelector('#PRICE_AREA_NAME, .area');
                        return el ? (el.value || el.innerText || '').trim() : '';
                    })()
                ''')
                if curr_area:
                    _state["sold_out_areas"].add(curr_area)
                    debug.log(f"[CTBC] Area '{curr_area}' sold out upon submission, added to cache")
                await tab.evaluate('''
                    (() => {
                        if (typeof backUrl !== 'undefined' && backUrl) {
                            location.href = backUrl;
                        } else {
                            top.history.go(-1);
                        }
                    })()
                ''')

        return tab

    # 6. Computer auto-assign or voucher area pages (UTK0201_001, UTK0202)
    if page_type in ("area_computer", "area_voucher"):
        if _state.get("cart_added"):
            debug.log("[CTBC] Cart addition already completed, waiting for navigation to checkout page (UTK0206_)...")
            return tab

        area_res = await nodriver_ctbc_area_auto_select(tab, config_dict)
        if area_res.get("status") == "navigated":
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

    # 7. Homepage / Login page (UTK0101_, UTK0102_)
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

        # Auto select match from homepage / list
        await nodriver_ctbc_home_auto_select(tab, config_dict)
        return tab

    return tab
