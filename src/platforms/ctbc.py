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
- Automatic ticket quantity assignment
- Captcha OCR recognition & auto-retry / refresh
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
    check_and_handle_pause,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    CONST_FROM_TOP_TO_BOTTOM,
)

__all__ = [
    "CTBC_URL_PATTERNS",
    "is_ctbc_url",
    "get_ctbc_page_type",
    "nodriver_ctbc_extract_captcha_base64",
    "nodriver_ctbc_login",
    "nodriver_ctbc_vip_login",
    "nodriver_ctbc_dismiss_dialog",
    "nodriver_ctbc_date_auto_select",
    "nodriver_ctbc_area_auto_select",
    "nodriver_ctbc_assign_ticket_number",
    "nodriver_ctbc_captcha_handler",
    "nodriver_ctbc_checkout",
    "nodriver_ctbc_main",
]

CTBC_URL_PATTERNS = {
    "domain": r"ctbcsports\.com",
    "checkout": r"utk0206",
    "event": r"utk0201_",
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
    "played_sound_order": False,
    "shown_checkout_message": False,
    "login_attempted": False,
    "vip_login_attempted": False,
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
    if "utk0204" in url_lower:
        return "area_table"
    if "utk0201_" in url_lower and "product_id=" in url_lower:
        return "event"
    if "utk130" in url_lower:
        return "login"
    if "utk0101" in url_lower or url_lower.rstrip("/").endswith("ctbcsports.com") or url_lower.endswith("/dea/") or url_lower.endswith("/brothers/"):
        return "home"
    return "other"


async def nodriver_ctbc_extract_captcha_base64(tab, selector='#chk_pic, #master_chk_pic, img[src*="pic?TYPE="]'):
    """Extract captcha image base64 bytes using canvas rendering."""
    try:
        script = f'''
            (() => {{
                const img = document.querySelector('{selector}');
                if (!img || !img.complete || img.naturalWidth === 0) return null;
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth || img.width;
                canvas.height = img.naturalHeight || img.height;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0);
                return canvas.toDataURL('image/png');
            }})()
        '''
        data_url = await tab.evaluate(script)
        if data_url and ',' in data_url:
            base64_data = data_url.split(',', 1)[1]
            return base64.b64decode(base64_data)
    except Exception:
        pass
    return None


async def nodriver_ctbc_dismiss_dialog(tab):
    """Close any blocking jQuery UI popup dialogs."""
    try:
        await tab.evaluate('''
            (() => {
                const dialogBtn = document.querySelector('.ui-dialog-buttonset button, .ui-dialog-titlebar-close');
                if (dialogBtn && dialogBtn.offsetParent !== null) {
                    dialogBtn.click();
                    return true;
                }
                return false;
            })()
        ''')
    except Exception:
        pass


async def nodriver_ctbc_login(tab, config_dict, ocr):
    """Handle member login for CTBC Sports (via popup dialog or page)."""
    debug = util.create_debug_logger(config_dict)

    # 1. Check if already logged in
    try:
        login_status_raw = await tab.evaluate('''
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
        login_status = util.parse_nodriver_result(login_status_raw)
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
            await tab.sleep(0.5)

        # Fill account and password
        await tab.evaluate(f'''
            (() => {{
                const accInput = document.querySelector('#MASTER_ACCOUNT');
                if (accInput) {{
                    accInput.value = "{account}";
                    accInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    accInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
                const pwdInput = document.querySelector('#MASTER_PASSWORD');
                if (pwdInput) {{
                    pwdInput.value = "{password}";
                    pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    pwdInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }})()
        ''')

        # Captcha OCR
        if ocr and config_dict.get("ocr_captcha", {}).get("enable", True):
            for retry in range(3):
                img_bytes = await nodriver_ctbc_extract_captcha_base64(tab, selector='#master_chk_pic')
                if img_bytes:
                    try:
                        ans = ocr.classification(img_bytes)
                        if ans:
                            ans = ans.strip()
                            debug.log(f"[CTBC LOGIN] OCR raw answer: {ans}")
                            if len(ans) == 4:
                                await tab.evaluate(f'''
                                    (() => {{
                                        const chk = document.querySelector('#MASTER_CHK');
                                        if (chk) {{
                                            chk.value = "{ans}";
                                            chk.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                            chk.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                        }}
                                    }})()
                                ''')
                                await tab.sleep(0.2)
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
                await tab.sleep(0.5)

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
        await tab.sleep(1.0)
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

        # Determine value to fill into ID1
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
                const id1 = document.querySelector('#ID1');
                if (id1 && !id1.value) {{
                    id1.value = "{id1_val}";
                    id1.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                const tr2 = document.querySelector('#TR2');
                const id2 = document.querySelector('#ID2');
                if (tr2 && window.getComputedStyle(tr2).display !== 'none' && id2 && !id2.value) {{
                    id2.value = "{id2_val}";
                    id2.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            }})()
        ''')

        # OCR #chk_pic
        if ocr and config_dict.get("ocr_captcha", {}).get("enable", True):
            for retry in range(3):
                img_bytes = await nodriver_ctbc_extract_captcha_base64(tab, selector='#chk_pic')
                if img_bytes:
                    try:
                        ans = ocr.classification(img_bytes)
                        if ans:
                            ans = ans.strip()
                            debug.log(f"[CTBC VIP] Captcha OCR answer: {ans}")
                            if len(ans) == 4:
                                await tab.evaluate(f'''
                                    (() => {{
                                        const chk = document.querySelector('#CHK');
                                        if (chk) {{
                                            chk.value = "{ans}";
                                            chk.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        }}
                                    }})()
                                ''')
                                await tab.sleep(0.2)
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
                await tab.sleep(0.5)

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
        await tab.sleep(1.0)
        return True

    except Exception as exc:
        debug.log(f"[CTBC VIP] VIP login exception: {exc}")
        return False


async def nodriver_ctbc_date_auto_select(tab, config_dict):
    """Auto-select performance date / session on UTK0201_ event page."""
    debug = util.create_debug_logger(config_dict)

    if not config_dict.get("date_auto_select", {}).get("enable", True):
        debug.log("[CTBC DATE] Date auto select disabled")
        return False

    date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "").strip()
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    date_auto_fallback = config_dict.get("date_auto_fallback", False)
    date_mode = config_dict.get("date_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)

    # 1. Ensure Performance list is populated
    try:
        table_state = await tab.evaluate('''
            (() => {
                const table = document.querySelector('#PerformanceListTable');
                if (!table) return { exists: false, count: 0 };
                const rows = table.querySelectorAll('tr');
                return { exists: true, count: rows.length };
            })()
        ''')
        if not table_state or table_state.get('count', 0) == 0:
            debug.log("[CTBC DATE] Performance list empty, calling ShowPerformance()...")
            await tab.evaluate('if (typeof ShowPerformance === "function") ShowPerformance();')
            await tab.sleep(1.0)
    except Exception as exc:
        debug.log(f"[CTBC DATE] Check table error: {exc}")

    # 2. Extract performance rows
    try:
        perf_data_raw = await tab.evaluate('''
            (() => {
                const rows = document.querySelectorAll('#PerformanceListTable tr, .ng-star-inserted tr');
                const list = [];
                rows.forEach((row, idx) => {
                    const text = row.innerText.trim().replace(/\\s+/g, ' ');
                    if (!text || text.includes('場次') && text.includes('時間')) return;
                    // Find clickable purchase / vip button
                    const btn = row.querySelector('button, a.learnmore, .btn, [onclick*="VipSellCheck"], [onclick*="doLink"], [onclick*="UTK"]');
                    const hasAction = !!btn || text.includes('立即') || text.includes('訂購') || text.includes('預購') || text.includes('購買');
                    list.push({
                        index: idx,
                        text: text,
                        hasAction: hasAction,
                        disabled: row.innerText.includes('已售完') || row.innerText.includes('暫無票券')
                    });
                });
                return list;
            })()
        ''')
        perf_items = util.parse_nodriver_result(perf_data_raw)
        if not perf_items or not isinstance(perf_items, list):
            debug.log("[CTBC DATE] No performance items parsed")
            return False

        debug.log(f"[CTBC DATE] Found {len(perf_items)} performance entries")

        # 3. Match candidate
        target_item = None
        keywords = util.parse_keyword_string_to_array(date_keyword) if date_keyword else []

        if keywords:
            for kw in keywords:
                kw_parts = kw.split(' ') if ' ' in kw else [kw]
                for item in perf_items:
                    if item.get("disabled"):
                        continue
                    text = item.get("text", "")
                    if keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, text):
                        continue
                    if all(part in text for part in kw_parts):
                        target_item = item
                        debug.log(f"[CTBC DATE] Matched performance '{text}' with keyword '{kw}'")
                        break
                if target_item:
                    break

        if not target_item:
            if date_auto_fallback:
                debug.log(f"[CTBC DATE] Keyword not matched, applying fallback mode '{date_mode}'")
                available = [
                    item for item in perf_items
                    if not item.get("disabled") and not (
                        keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, item.get("text", ""))
                    )
                ]
                if available:
                    target_item = util.get_target_item_from_matched_list(available, date_mode)
                    if target_item:
                        debug.log(f"[CTBC DATE] Fallback selected performance: {target_item.get('text')}")
            else:
                debug.log("[CTBC DATE] Strict mode: no fallback selected")
                return False

        if target_item:
            idx = target_item.get("index", 0)
            debug.log(f"[CTBC DATE] Clicking target performance row index {idx}...")
            await tab.evaluate(f'''
                (() => {{
                    const rows = document.querySelectorAll('#PerformanceListTable tr, .ng-star-inserted tr');
                    const row = rows[{idx}];
                    if (!row) return false;
                    const btn = row.querySelector('button, a.learnmore, .btn, [onclick*="VipSellCheck"], [onclick*="doLink"], [onclick*="UTK"]');
                    if (btn) {{
                        btn.click();
                        return true;
                    }}
                    row.click();
                    return true;
                }})()
            ''')
            await tab.sleep(1.0)
            return True

    except Exception as exc:
        debug.log(f"[CTBC DATE] Date select exception: {exc}")
        return False

    return False


async def nodriver_ctbc_area_auto_select(tab, config_dict):
    """Auto-select ticket price area on area pages."""
    debug = util.create_debug_logger(config_dict)

    if not config_dict.get("area_auto_select", {}).get("enable", True):
        debug.log("[CTBC AREA] Area auto select disabled")
        return False

    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "").strip()
    keyword_exclude = config_dict.get("keyword_exclude", "").strip()
    area_auto_fallback = config_dict.get("area_auto_fallback", False)
    area_mode = config_dict.get("area_auto_select", {}).get("mode", CONST_FROM_TOP_TO_BOTTOM)

    try:
        areas_raw = await tab.evaluate('''
            (() => {
                // Table rows or cards
                const rows = document.querySelectorAll('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item');
                const list = [];
                rows.forEach((row, idx) => {
                    const text = row.innerText.trim().replace(/\\s+/g, ' ');
                    if (!text || text.includes('票價') && text.includes('剩餘')) return;
                    const isSoldOut = row.classList.contains('Soldout') || text.includes('已售完') || text.includes('0張');
                    list.push({
                        index: idx,
                        text: text,
                        soldOut: isSoldOut
                    });
                });
                return list;
            })()
        ''')
        area_items = util.parse_nodriver_result(areas_raw)
        if not area_items or not isinstance(area_items, list):
            debug.log("[CTBC AREA] No area rows found on page")
            return False

        target_area = None
        keywords = util.parse_keyword_string_to_array(area_keyword) if area_keyword else []

        if keywords:
            for kw in keywords:
                kw_parts = kw.split(' ') if ' ' in kw else [kw]
                for area in area_items:
                    if area.get("soldOut"):
                        continue
                    text = area.get("text", "")
                    if keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, text):
                        continue
                    if all(part in text for part in kw_parts):
                        target_area = area
                        debug.log(f"[CTBC AREA] Matched area '{text}' with keyword '{kw}'")
                        break
                if target_area:
                    break

        if not target_area:
            if area_auto_fallback:
                debug.log(f"[CTBC AREA] Applying fallback mode '{area_mode}'")
                available = [
                    a for a in area_items
                    if not a.get("soldOut") and not (
                        keyword_exclude and util.reset_row_text_if_match_keyword_exclude(config_dict, a.get("text", ""))
                    )
                ]
                if available:
                    target_area = util.get_target_item_from_matched_list(available, area_mode)
                    if target_area:
                        debug.log(f"[CTBC AREA] Fallback selected area: {target_area.get('text')}")
            else:
                debug.log("[CTBC AREA] Strict mode: no fallback selected")
                return False

        if target_area:
            idx = target_area.get("index", 0)
            debug.log(f"[CTBC AREA] Clicking area index {idx}...")
            await tab.evaluate(f'''
                (() => {{
                    const rows = document.querySelectorAll('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item');
                    const row = rows[{idx}];
                    if (row) {{
                        const btn = row.querySelector('button, a, input[type="button"]');
                        if (btn) btn.click();
                        else row.click();
                        return true;
                    }}
                    return false;
                }})()
            ''')
            await tab.sleep(0.5)
            return True

    except Exception as exc:
        debug.log(f"[CTBC AREA] Area selection exception: {exc}")
        return False

    return False


async def nodriver_ctbc_assign_ticket_number(tab, config_dict):
    """Set the desired ticket quantity."""
    ticket_number = str(config_dict.get("ticket_number", 1))
    debug = util.create_debug_logger(config_dict)

    try:
        await tab.evaluate(f'''
            (() => {{
                const targetQty = '{ticket_number}';
                // 1. Check AMOUNT input
                const inputs = document.querySelectorAll('#AMOUNT, input.numbox, input.yd_counterNum, div.qty-select input');
                inputs.forEach(input => {{
                    if (input && (input.value === '' || input.value === '0')) {{
                        input.value = targetQty;
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }});

                // 2. Check SELECT dropdown
                const selects = document.querySelectorAll('select#AMOUNT, select.ticket-qty, select[name*="amount"]');
                selects.forEach(sel => {{
                    if (sel) {{
                        sel.value = targetQty;
                        sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }});
            }})()
        ''')
        debug.log(f"[CTBC COUNT] Ticket quantity set to {ticket_number}")
        return True
    except Exception as exc:
        debug.log(f"[CTBC COUNT] Set quantity exception: {exc}")
        return False


async def nodriver_ctbc_captcha_handler(tab, config_dict, ocr):
    """Detect, solve, and submit captcha on purchase/area pages."""
    debug = util.create_debug_logger(config_dict)

    if not ocr or not config_dict.get("ocr_captcha", {}).get("enable", True):
        return False

    img_bytes = await nodriver_ctbc_extract_captcha_base64(tab, selector='#chk_pic, img[src*="pic?TYPE="]')
    if not img_bytes:
        return False

    debug.log("[CTBC CAPTCHA] Found captcha image, running OCR...")
    try:
        ans = ocr.classification(img_bytes)
        if ans:
            ans = ans.strip()
            debug.log(f"[CTBC CAPTCHA] OCR result: {ans}")
            if len(ans) == 4:
                await tab.evaluate(f'''
                    (() => {{
                        const input = document.querySelector('#CHK, #AMOUNT_CHK, input[name*="chk"]');
                        if (input) {{
                            input.value = "{ans}";
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    }})()
                ''')
                await tab.sleep(0.2)
                return True
            else:
                # Refresh captcha
                await tab.evaluate('''
                    (() => {
                        const changPic = document.querySelector('#chang_pic');
                        if (changPic) changPic.click();
                    })()
                ''')
                await tab.sleep(0.5)
    except Exception as exc:
        debug.log(f"[CTBC CAPTCHA] Exception during OCR: {exc}")

    return False


async def nodriver_ctbc_checkout(tab, config_dict):
    """
    Handle the shopping cart and checkout process on UTK0206_.
    Stages:
    1. Check cart items
    2. Click '前往結帳' (#Checkout) if #paybill is hidden
    3. Select pickup method (#GET_METOD_ROOT, prefer 128 APP e-ticket)
    4. Select payment method (#PAY_METOD_ROOT, prefer 1 Credit card)
    5. Fill credit card fields (if applicable)
    6. Check agreement terms (#agreen)
    7. Click '送出結帳' (chkNext)
    8. Send notifications & play sound
    """
    debug = util.create_debug_logger(config_dict)
    now = time.time()

    if _state.get("checkout_submitted"):
        if (now - _state.get("checkout_submitted_time", 0.0)) < CONST_CTBC_SUBMIT_COOLDOWN:
            debug.log("[CTBC CHECKOUT] Checkout submission in cooldown, waiting for response...")
            return True

    debug.log("[CTBC CHECKOUT] Processing UTK0206_ checkout page...")

    # Step 1: Check if cart has items
    has_items = await tab.evaluate('''
        (() => {
            const normal = document.querySelector('#normalTicket, .orders');
            const pkg = document.querySelector('#packageTicket');
            const season = document.querySelector('#seasonTicket');
            const text = (normal ? normal.innerText : '') + (pkg ? pkg.innerText : '') + (season ? season.innerText : '');
            return text.trim().length > 0;
        })()
    ''')
    if not has_items:
        debug.log("[CTBC CHECKOUT] No tickets found in cart yet")
        return False

    # Step 2: Ensure #paybill is opened (click #Checkout if necessary)
    is_paybill_ready = await tab.evaluate('''
        (() => {
            const paybill = document.querySelector('#paybill');
            if (paybill && window.getComputedStyle(paybill).display !== 'none') {
                const getMethods = document.querySelectorAll('input[name="howtoGet"]');
                return getMethods.length > 0;
            }
            return false;
        })()
    ''')

    if not is_paybill_ready:
        debug.log("[CTBC CHECKOUT] Clicking #Checkout ('前往結帳')...")
        await tab.evaluate('''
            (() => {
                const checkoutBtn = document.querySelector('#Checkout');
                if (checkoutBtn) {
                    checkoutBtn.click();
                    return true;
                }
                return false;
            })()
        ''')
        await tab.sleep(1.0)

    # Step 3: Select pickup method (取票方式)
    # 128 = APP電子票 (0元服務費), 64 = 7-11 ibon, 1 = 現場取票, 4 = 現場入口取票
    pickup_pref = config_dict.get("ctbc", {}).get("pickup_method", "128")
    await tab.evaluate(f'''
        (() => {{
            const checkedGet = document.querySelector('input[name="howtoGet"]:checked');
            if (checkedGet) return;

            // Preferred method
            let target = document.querySelector('#GetMethods{pickup_pref}');
            if (!target) {{
                // Default to 128 (APP) if present
                target = document.querySelector('#GetMethods128');
            }}
            if (!target) {{
                // Fallback to first available
                target = document.querySelector('input[name="howtoGet"]');
            }}
            if (target) {{
                target.click();
            }}
        }})()
    ''')
    await tab.sleep(0.3)

    # Step 4: Select payment method (付款方式)
    # 1 = 信用卡, 32 = ATM 虛擬帳號
    pay_pref = config_dict.get("ctbc", {}).get("payment_method", "1")
    await tab.evaluate(f'''
        (() => {{
            const checkedPay = document.querySelector('input[name="pay"]:checked');
            if (checkedPay) return;

            let target = document.querySelector('#PayMethods{pay_pref}');
            if (!target) {{
                target = document.querySelector('#PayMethods1');
            }}
            if (!target) {{
                target = document.querySelector('input[name="pay"]');
            }}
            if (target) {{
                target.click();
            }}
        }})()
    ''')
    await tab.sleep(0.3)

    # Step 5: Fill credit card fields if Credit Card payment (1) is active
    card_number = config_dict.get("contact", {}).get("credit_card_prefix", "").strip()
    # If 16 digits provided in config, fill it
    if len(card_number) == 16:
        await tab.evaluate(f'''
            (() => {{
                const cardInput = document.querySelector('#CARD_NUMBER');
                if (cardInput && !cardInput.value) {{
                    cardInput.value = "{card_number}";
                    cardInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            }})()
        ''')

    # Step 6: Check agreement terms (#agreen)
    await tab.evaluate('''
        (() => {
            const agreen = document.querySelector('#agreen');
            if (agreen && !agreen.checked) {
                agreen.checked = true;
                agreen.dispatchEvent(new Event('change', { bubbles: true }));
                agreen.click();
            }
        })()
    ''')
    await tab.sleep(0.2)

    # Step 7: Click Submit Checkout (送出結帳 / chkNext)
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

        # Step 8: Play sound & send notifications (once)
        if not _state["played_sound_order"]:
            if config_dict.get("advanced", {}).get("play_sound", {}).get("order", True):
                play_sound_while_ordering(config_dict)
            send_discord_notification(config_dict, "order", "CTBC Sports")
            send_telegram_notification(config_dict, "order", "CTBC Sports")
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

    # Dismiss any unhandled alert dialogs
    await nodriver_ctbc_dismiss_dialog(tab)

    # 1. Checkout page (UTK0206_)
    if page_type == "checkout":
        await nodriver_ctbc_checkout(tab, config_dict)
        return tab

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
        # Select Area
        await nodriver_ctbc_area_auto_select(tab, config_dict)

        # Set ticket quantity
        await nodriver_ctbc_assign_ticket_number(tab, config_dict)

        # Handle Captcha if present
        is_captcha_solved = await nodriver_ctbc_captcha_handler(tab, config_dict, ocr)

        # Add to cart
        if is_captcha_solved or not config_dict.get("ocr_captcha", {}).get("enable", True):
            debug.log("[CTBC] Clicking add to cart...")
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
            await tab.sleep(1.0)
        return tab

    # 5. Homepage / Login page
    if page_type in ("home", "login"):
        await nodriver_ctbc_login(tab, config_dict, ocr)

        # If user configured a specific event page as homepage, redirect to it
        cfg_homepage = config_dict.get("homepage", "").strip()
        if cfg_homepage and cfg_homepage.lower() != url.lower() and "utk0201_" in cfg_homepage.lower():
            debug.log(f"[CTBC] Navigating from home to configured event page: {cfg_homepage}")
            try:
                await tab.get(cfg_homepage)
            except Exception as e:
                debug.log(f"[CTBC] Navigation error: {e}")
        return tab

    return tab
