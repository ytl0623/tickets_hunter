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
    "checkout_halted": False,
    "shown_halt_message": False,
    "played_sound_order": False,
    "shown_checkout_message": False,
    "login_attempted": False,
    "vip_login_attempted": False,
    "last_cart_submit_time": 0.0,
    "last_captcha_src": "",
    "last_captcha_ans": "",
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
    """Extract captcha image base64 bytes using canvas rendering with solid white background."""
    try:
        script = f'''
            (() => {{
                const img = document.querySelector('{selector}');
                if (!img || !img.complete || (img.naturalWidth === 0 && img.width === 0)) return null;
                const canvas = document.createElement('canvas');
                const w = img.naturalWidth || img.width;
                const h = img.naturalHeight || img.height;
                if (!w || !h) return null;
                canvas.width = w;
                canvas.height = h;
                const ctx = canvas.getContext('2d');
                // Fill opaque white background to prevent dark transparent PNG OCR artifacts
                ctx.fillStyle = '#FFFFFF';
                ctx.fillRect(0, 0, w, h);
                ctx.drawImage(img, 0, 0, w, h);
                return {{
                    dataUrl: canvas.toDataURL('image/png'),
                    src: img.src || ''
                }};
            }})()
        '''
        res_raw = await tab.evaluate(script)
        res = util.parse_nodriver_result(res_raw)
        if isinstance(res, dict) and res.get('dataUrl') and ',' in res['dataUrl']:
            base64_data = res['dataUrl'].split(',', 1)[1]
            return base64.b64decode(base64_data), res.get('src', '')
    except Exception:
        pass
    return None, ''


async def nodriver_ctbc_dismiss_dialog(tab, config_dict=None):
    """Close any blocking jQuery UI popup dialogs and log the alert message."""
    debug = util.create_debug_logger(config_dict) if config_dict else None
    try:
        dialog_data_raw = await tab.evaluate('''
            (() => {
                const dialogMsg = document.querySelector('#dialog-message, .ui-dialog-content');
                let text = '';
                if (dialogMsg) {
                    text = dialogMsg.innerText.trim();
                }
                const dialogBtn = document.querySelector('.ui-dialog-buttonset button, .ui-dialog-titlebar-close');
                if (dialogBtn && dialogBtn.offsetParent !== null) {
                    dialogBtn.click();
                    return { dismissed: true, text: text };
                }
                return { dismissed: false, text: text };
            })()
        ''')
        data = util.parse_nodriver_result(dialog_data_raw)
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
                img_bytes, _ = await nodriver_ctbc_extract_captcha_base64(tab, selector='#master_chk_pic')
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
                img_bytes, _ = await nodriver_ctbc_extract_captcha_base64(tab, selector='#chk_pic')
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
                    // Skip table headers and rows inside thead
                    if (row.querySelector('th') || row.closest('thead')) return;
                    const text = row.innerText.trim().replace(/\\s+/g, ' ');
                    if (!text || (text.includes('地點') && text.includes('名稱'))) return;
                    // Find clickable purchase / vip button
                    const btn = row.querySelector('button, a.learnmore, .btn, [onclick*="VipSellCheck"], [onclick*="doLink"], [onclick*="UTK"]');
                    if (!btn && !text.includes('立即') && !text.includes('訂購') && !text.includes('預購')) return;
                    list.push({
                        index: idx,
                        text: text,
                        hasAction: !!btn,
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
        # Check if already on direct ticket count page (e.g. UTK0202_) without area list
        is_direct_qty_page = await tab.evaluate('''
            (() => {
                const hasAmountInput = document.querySelector('#AMOUNT, input[KEY="TYPE_ID"], #table_tickettype');
                const hasAreaElements = document.querySelector('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item, select#PRICE');
                return {
                    isDirect: !!hasAmountInput && !hasAreaElements
                };
            })()
        ''')
        page_info = util.parse_nodriver_result(is_direct_qty_page)
        if isinstance(page_info, dict) and page_info.get("isDirect"):
            debug.log("[CTBC AREA] Direct ticket quantity page (no area table required)")
            return True

        areas_raw = await tab.evaluate('''
            (() => {
                // 1. Check select#PRICE dropdown
                const priceSelect = document.querySelector('select#PRICE');
                if (priceSelect && priceSelect.options.length > 1) {
                    const selectList = [];
                    for (let i = 0; i < priceSelect.options.length; i++) {
                        const opt = priceSelect.options[i];
                        if (opt.value && opt.value !== '-1') {
                            selectList.push({
                                index: i,
                                text: opt.text.trim(),
                                isSelect: true,
                                soldOut: opt.text.includes('售完') || opt.text.includes('0張')
                            });
                        }
                    }
                    if (selectList.length > 0) return selectList;
                }

                // 2. Table rows or cards
                const rows = document.querySelectorAll('table.salesTable tr, tr.main, table tr[onclick], tr.status_tr, .area_item');
                const list = [];
                rows.forEach((row, idx) => {
                    const text = row.innerText.trim().replace(/\\s+/g, ' ');
                    if (!text || text.includes('票價') && text.includes('剩餘')) return;
                    const isSoldOut = row.classList.contains('Soldout') || text.includes('已售完') || text.includes('0張');
                    list.push({
                        index: idx,
                        text: text,
                        isSelect: false,
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
            is_sel = target_area.get("isSelect", False)
            debug.log(f"[CTBC AREA] Selecting area index {idx} (isSelect={is_sel})...")
            await tab.evaluate(f'''
                (() => {{
                    if ({str(is_sel).lower()}) {{
                        const priceSelect = document.querySelector('select#PRICE');
                        if (priceSelect && priceSelect.selectedIndex !== {idx}) {{
                            priceSelect.selectedIndex = {idx};
                            priceSelect.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            if (typeof changeArea === 'function') changeArea();
                            return true;
                        }}
                        return true;
                    }}
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
    """Set the desired ticket quantity, respecting QUANTITY_LIMIT and FIRST_QTY_LIMIT."""
    requested_qty = int(config_dict.get("ticket_number", 1))
    debug = util.create_debug_logger(config_dict)

    try:
        qty_result_raw = await tab.evaluate(f'''
            (() => {{
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

                let changed = false;
                // 1. Check AMOUNT input
                const inputs = document.querySelectorAll('#AMOUNT, input.numbox, input.yd_counterNum, div.qty-select input');
                inputs.forEach(input => {{
                    if (input && input.value !== targetQty.toString()) {{
                        input.value = targetQty.toString();
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                        changed = true;
                    }}
                }});

                // 2. Check SELECT dropdown
                const selects = document.querySelectorAll('select#AMOUNT, select.ticket-qty, select[name*="amount"]');
                selects.forEach(sel => {{
                    if (sel && sel.value !== targetQty.toString()) {{
                        sel.value = targetQty.toString();
                        sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        changed = true;
                    }}
                }});

                return {{
                    requested: reqQty,
                    assigned: targetQty,
                    limit: maxLimit,
                    changed: changed
                }};
            }})()
        ''')
        qty_info = util.parse_nodriver_result(qty_result_raw)
        if isinstance(qty_info, dict):
            assigned = qty_info.get("assigned", requested_qty)
            limit = qty_info.get("limit", 99)
            if assigned < requested_qty:
                debug.log(f"[CTBC COUNT] Requested {requested_qty} tickets, but page limit is {limit}. Adjusted quantity to {assigned}.")
            else:
                debug.log(f"[CTBC COUNT] Ticket quantity set to {assigned}")
            return True
    except Exception as exc:
        debug.log(f"[CTBC COUNT] Set quantity exception: {exc}")
        return False


async def nodriver_ctbc_captcha_handler(tab, config_dict, ocr):
    """Detect, solve, and submit captcha on purchase/area pages."""
    debug = util.create_debug_logger(config_dict)

    if not ocr or not config_dict.get("ocr_captcha", {}).get("enable", True):
        return False

    img_bytes, img_src = await nodriver_ctbc_extract_captcha_base64(tab, selector='#chk_pic, img[src*="pic?TYPE="]')
    if not img_bytes:
        return False

    # Check current input value
    current_val_raw = await tab.evaluate('''
        (() => {
            const input = document.querySelector('#CHK, #AMOUNT_CHK, input[name*="chk"]');
            return input ? input.value.trim() : '';
        })()
    ''')
    current_ans = util.parse_nodriver_result(current_val_raw)
    if _state.get("last_captcha_src") == img_src and current_ans and len(current_ans) == 4:
        # Same image already solved and input field filled
        return True

    debug.log("[CTBC CAPTCHA] Found captcha image, running OCR...")
    try:
        ans = ocr.classification(img_bytes)
        if ans:
            ans = ans.strip()
            debug.log(f"[CTBC CAPTCHA] OCR result: {ans}")
            if len(ans) == 4:
                _state["last_captcha_src"] = img_src
                _state["last_captcha_ans"] = ans
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
                debug.log(f"[CTBC CAPTCHA] OCR length mismatch ({len(ans)} != 4), refreshing captcha...")
                await tab.evaluate('''
                    (() => {
                        const changPic = document.querySelector('#chang_pic, #master_chang_pic');
                        if (changPic) changPic.click();
                    })()
                ''')
                await tab.sleep(0.6)
    except Exception as exc:
        debug.log(f"[CTBC CAPTCHA] Exception during OCR: {exc}")

    return False


async def nodriver_ctbc_checkout(tab, config_dict):
    """
    Handle the shopping cart and checkout process on UTK0206_.
    Stages:
    1. Check cart items / ready state
    2. Click '前往結帳' (#Checkout) if #paybill is hidden
    3. Select pickup method (#GET_METOD_ROOT, prefer 128 APP e-ticket)
    4. Select payment method (#PAY_METOD_ROOT, prefer 1 Credit card)
    5. Fill credit card fields (if applicable)
    6. Check the bottom two checkboxes:
       - Agreement terms (#agreen)
       - Sports coins (#M_DONGZI_COUPONS)
    7. Halt automation ('之後就不要動') and notify user to complete checkout manually.
       (Or submit via chkNext if auto_submit_checkout is explicitly enabled)
    """
    debug = util.create_debug_logger(config_dict)

    if _state.get("checkout_halted"):
        return True

    debug.log("[CTBC CHECKOUT] Processing UTK0206_ checkout page...")

    # Step 1: Check if cart has items
    has_items = await tab.evaluate('''
        (() => {
            const cartCount = document.querySelector('.cartCount');
            if (cartCount && parseInt(cartCount.innerText.trim()) > 0) return true;
            const normal = document.querySelector('#normalTicket, .orders');
            const pkg = document.querySelector('#packageTicket');
            const season = document.querySelector('#seasonTicket');
            const text = (normal ? normal.innerText : '') + (pkg ? pkg.innerText : '') + (season ? season.innerText : '');
            if (text.trim().length > 0) return true;
            const paybill = document.querySelector('#paybill');
            if (paybill && window.getComputedStyle(paybill).display !== 'none') return true;
            const checkoutBtn = document.querySelector('#Checkout');
            if (checkoutBtn) return true;
            return false;
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
        await tab.sleep(0.8)

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
    # 1 = 信用卡, 32 = ATM 虛擬帳號 (Note: 運動幣規定需搭配信用卡付款)
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

    # Step 6: 勾選最下面兩個勾選項目 (Check the bottom two checkboxes)
    # 1. 約定條款 (#agreen)
    # 2. 運動幣 (#M_DONGZI_COUPONS)
    checkbox_result_raw = await tab.evaluate('''
        (() => {
            let agreenFound = false;
            let dongziFound = false;
            let agreenChecked = false;
            let dongziChecked = false;

            // 1. #agreen
            const agreen = document.querySelector('#agreen');
            if (agreen) {
                agreenFound = true;
                if (!agreen.checked) {
                    agreen.checked = true;
                    agreen.dispatchEvent(new Event('input', { bubbles: true }));
                    agreen.dispatchEvent(new Event('change', { bubbles: true }));
                }
                agreenChecked = agreen.checked;
            }

            // 2. #M_DONGZI_COUPONS
            const dongzi = document.querySelector('#M_DONGZI_COUPONS');
            if (dongzi) {
                dongziFound = true;
                if (!dongzi.checked) {
                    dongzi.click();
                    if (!dongzi.checked) {
                        dongzi.checked = true;
                        dongzi.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
                // Call InitDongZi if defined and sports deposit table not yet populated
                if (typeof InitDongZi === 'function') {
                    const sportsTable = document.querySelector('#sportsdeposit2');
                    if (!sportsTable || sportsTable.children.length === 0) {
                        InitDongZi();
                    }
                }
                dongziChecked = dongzi.checked;
            }

            // 3. Fallback: also ensure the last two checkboxes on the checkout page are checked
            const allCbs = Array.from(document.querySelectorAll('#paybill input[type="checkbox"], .checkRead input[type="checkbox"]'));
            if (allCbs.length >= 2) {
                const lastTwo = allCbs.slice(-2);
                lastTwo.forEach(cb => {
                    if (!cb.checked) {
                        cb.click();
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                });
            }

            return {
                agreenFound: agreenFound,
                dongziFound: dongziFound,
                agreenChecked: agreenChecked,
                dongziChecked: dongziChecked,
                totalCheckboxes: allCbs.length
            };
        })()
    ''')
    cb_info = util.parse_nodriver_result(checkbox_result_raw)
    debug.log(f"[CTBC CHECKOUT] Checked bottom checkboxes status: {cb_info}")

    # Check if user explicitly wants auto-submit
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
        # "之後就不要動" -> Halt automation on checkout page and notify user to complete manually
        _state["checkout_halted"] = True
        debug.log("[SUCCESS] [CTBC CHECKOUT] 結帳頁面最下方兩個選項已勾選完成（約定條款 #agreen、運動幣 #M_DONGZI_COUPONS）！")
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
        if "加入購物車失敗" in dismissed_alert or "驗證碼" in dismissed_alert:
            await tab.evaluate('''
                (() => {
                    const changPic = document.querySelector('#chang_pic, #master_chang_pic');
                    if (changPic) changPic.click();
                })()
            ''')
            await tab.sleep(0.8)

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

        # Add to cart with in-flight check and cooldown
        now = time.time()
        last_submit = _state.get("last_cart_submit_time", 0.0)
        cooldown_ok = (now - last_submit) >= 2.0

        if (is_captcha_solved or not config_dict.get("ocr_captcha", {}).get("enable", True)) and cooldown_ok:
            is_pending = await tab.evaluate('typeof isClick !== "undefined" && isClick === true')
            if is_pending:
                debug.log("[CTBC] addShoppingCart() request is already in-flight, waiting...")
                return tab

            debug.log("[CTBC] Clicking add to cart...")
            _state["last_cart_submit_time"] = now
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
            await tab.sleep(1.2)
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
