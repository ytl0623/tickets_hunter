#encoding=utf-8
"""
Melon Ticket Global (tkglobal.melon.com) platform module.

Flow:
  Step 1  performance/index.htm
          -> select date  (#list_date > li.item_date)
          -> select time  (#list_time > li.item_time)
          -> click Get Tickets (button.reservationBtn) with a TRUSTED CDP click
  Step 2  reservation/popup/onestop.htm -> iframe #oneStopFrame
          stepTicket        : set quantity, press Next
          stepDelivery/Pay  : mobile, International Credit Card + VISA,
                              agree to all terms, then Checkout (opt-in)
          -> STOP at the payment gateway. Card details stay with the user.

Five findings drive this design. Each one cost a debugging session; none of
them should be "simplified" away:

1. element.click() on Get Tickets dispatches an untrusted event and grants no
   user activation, so Melon's handler runs but its window.open is refused --
   the click reports success and nothing happens. Only CDP input works there.
2. onestop.htm is an empty shell; every booking control lives inside
   #oneStopFrame (same origin). A document-level query finds nothing at all.
3. That iframe starts at about:blank, where readyState is already "complete".
   Readiness must be judged by the frame URL, not readyState, or the first
   probe latches onto an empty document and never runs again.
4. onestop.htm calls alert() for login / sales-period / service errors. A
   native alert freezes JS, so every tab.evaluate times out and the main loop
   goes blind. The dialog handler is mandatory, not a nicety.
5. The iframe keeps its stepTicket URL until the POST completes, so a submit
   button with no cooldown gets pressed dozens of times per second by the 50ms
   main loop. That earned a 403 from Melon's WAF. Every submitting click in
   this module goes through _submit_once().

Seat selection is not automated: the observed products (flplanTypeCode=DR0003)
submit straight to stepTicket with no seat step. DR0001 / DR0002 products open
stepSeat / stepBlock, whose DOM has not been captured -- the module stops and
says so rather than guessing at selectors.

Dependency: util.py, nodriver_common.py (no cross-platform imports)
"""

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

from zendriver import cdp

import util
from nodriver_common import (
    check_and_handle_pause,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    _cdp_click,
)

__all__ = [
    "nodriver_melon_main",
    "is_melon_url",
    "is_melon_booking_url",
]


# ===== Constants =====

CONST_MELON_PLATFORM_NAME = "Melon Ticket"
CONST_MELON_PERFORMANCE_PATH = "/performance/index.htm"

CONST_MELON_BOOKING_URL_HINTS = ("/reservation/popup/", "onestop", "stepticket",
                                 "stepdelivery", "steppay", "stepseat",
                                 "stepblock", "stepfinish")

CONST_MELON_SOLD_OUT_KEYWORDS = [
    "No Tickets", "No Ticket", "Sold Out", "sold out",
    "매진", "売り切れ", "售罄", "已售完",
]

# Button / option labels. English first (the module drives langCd=EN); other
# locales kept so a user who switches language is not silently unsupported.
CONST_MELON_NEXT_TEXTS = ["Next", "다음", "次へ", "下一步"]
CONST_MELON_CHECKOUT_TEXTS = ["Checkout", "Check out", "결제하기", "決済"]
CONST_MELON_PAYMENT_TEXTS = ["International Credit Card", "해외 신용카드",
                             "海外クレジットカード"]
CONST_MELON_CARD_TEXTS = ["VISA", "Visa", "비자"]
CONST_MELON_AGREE_ALL_TEXTS = ["Agreed to all terms", "Agree to all terms",
                               "Agree to all", "전체 동의", "모두 동의"]
CONST_MELON_PHONE = "0912345678"
CONST_MELON_AUTO_CHECKOUT = True

# Throttle intervals (seconds)
CONST_MELON_RESERVE_CLICK_INTERVAL = 3.0
CONST_MELON_LOG_INTERVAL = 5.0
# No booking window within this window means the Get Tickets click did not
# take; retry rather than idling forever, but slowly (netfunnel queue).
CONST_MELON_RESERVE_RETRY_AFTER = 30.0
# How long to wait for a form submission to navigate before reporting it stuck.
CONST_MELON_SUBMIT_COOLDOWN = 8.0
# How long after that before saying so again.
CONST_MELON_STUCK_LOG_INTERVAL = 15.0

# Module-level state (cleared when the user switches to another prodId)
_state = {}


# ===== URL helpers =====

def is_melon_url(url):
    return "melon.com" in (url or "")


def is_melon_performance_url(url):
    return CONST_MELON_PERFORMANCE_PATH in (url or "")


def is_melon_booking_url(url):
    url_lower = (url or "").lower()
    if not is_melon_url(url_lower):
        return False
    if is_melon_performance_url(url_lower):
        return False
    return any(hint in url_lower for hint in CONST_MELON_BOOKING_URL_HINTS)


def _is_melon_login_url(url):
    url_lower = (url or "").lower()
    return "gmember.melon.com" in url_lower or "gaccounts.melon.com" in url_lower


def _throttled(key, interval):
    """True when the caller should act (and marks the timestamp)."""
    now = time.time()
    if now - _state.get(key, 0) < interval:
        return False
    _state[key] = now
    return True


def _reset_state_for_product(url):
    """Clear per-run latches when the user switches to another event."""
    prod_id = ""
    if "prodId=" in url:
        prod_id = url.split("prodId=")[-1].split("&")[0]
    if not prod_id or _state.get("prod_id") == prod_id:
        return
    _state.clear()
    _state["prod_id"] = prod_id


# ===== Alert handling =====

async def _register_alert_handler(tab, config_dict):
    """Dismiss Melon's alert() dialogs and surface their text.

    These messages ("Before booking, please log in.", "Not the sales period.",
    "Service is delayed.") are the most useful diagnostic this platform emits,
    and an undismissed alert freezes all JS evaluation.
    """
    if _state.get("alert_handler_registered"):
        return
    debug = util.create_debug_logger(config_dict)

    async def handle_melon_alert(event):
        message = getattr(event, "message", "")
        print(f"[MELON ALERT] {message}")
        _state["last_alert"] = message
        _state["last_alert_time"] = time.time()
        for _ in range(3):
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
                return
            except Exception as exc:
                text = str(exc)
                if "No dialog is showing" in text or "-32602" in text:
                    return
                await asyncio.sleep(0.1)

    try:
        tab.add_handler(cdp.page.JavascriptDialogOpening, handle_melon_alert)
        _state["alert_handler_registered"] = True
        debug.log("[MELON ALERT] handler registered")
    except Exception as exc:
        debug.log(f"[MELON ALERT] handler register failed: {exc}")


async def _wait_for_manual_login(tab):
    """Suspend the main loop until the user finishes the Melon login.

    A login page can remain open for minutes.  The normal 50 ms platform loop
    would otherwise repeatedly inspect it, so use CDP's navigation event as
    the wake-up signal instead of polling the page while the user logs in.
    """
    login_event = getattr(tab, "_melon_login_event", None)
    if login_event is None:
        login_event = asyncio.Event()

        async def handle_navigation(event):
            frame = getattr(event, "frame", None)
            # Only a top-level navigation proves that the login flow finished;
            # frames inside the identity-provider page navigate independently.
            if frame and getattr(frame, "parent_id", None) is None:
                if not _is_melon_login_url(getattr(frame, "url", "")):
                    login_event.set()

        tab.add_handler(cdp.page.FrameNavigated, handle_navigation)
        # add_handler only stores the callback; zendriver sends Page.enable
        # from the next send().  This function awaits without sending anything,
        # so enable the domain here or no event ever arrives and the wait hangs.
        await tab.send(cdp.page.enable())
        setattr(tab, "_melon_login_event", login_event)

    # Clear a signal from an earlier successful login before beginning a new
    # manual-login wait.  If navigation won the race, the URL check avoids
    # waiting after the user has already left the page.
    login_event.clear()
    current_url = str(getattr(getattr(tab, "target", None), "url", "") or "")
    if not _is_melon_login_url(current_url):
        return

    print("[MELON] 目前在 Melon 登入頁，請手動登入；登入完成後會自動繼續")
    while True:
        try:
            await asyncio.wait_for(login_event.wait(), timeout=1.0)
            break
        except asyncio.TimeoutError:
            # Fallback: a login finished in a separate popup never navigates
            # this tab, so no FrameNavigated arrives.  target.url is a CDP
            # cached value that costs nothing, and the 1 s tick keeps the
            # stop/pause controls responsive while the wait is in progress.
            live_url = str(getattr(getattr(tab, "target", None), "url", "") or "")
            if not _is_melon_login_url(live_url):
                break
    print("[MELON] 偵測到登入完成，繼續執行")


# ===== Keyword matching =====

def _parse_keyword_array(keyword_str):
    """settings syntax: "a","b"  or  ["a","b"],"c"  (AND inside a list)."""
    keyword_str = (keyword_str or "").strip()
    if not keyword_str:
        return []
    try:
        parsed = json.loads("[" + keyword_str + "]")
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return [keyword_str]


def _is_match(text, keyword_item):
    normalized = re.sub(r"\s+", " ", text)
    if isinstance(keyword_item, list):
        return all(re.sub(r"\s+", " ", str(k)) in normalized for k in keyword_item)
    return re.sub(r"\s+", " ", str(keyword_item)) in normalized


def _pick_index(rows, keyword_str, mode, fallback, config_dict, debug, label):
    """Index of the row to click, or None.

    Keyword groups are checked in priority order (first hit wins), matching the
    KKTIX date-selection semantics so users only learn one keyword syntax.
    """
    candidates = []
    for row in rows:
        if not row.get("selectable"):
            continue
        # Truthy return means the row matched an exclude keyword.
        if util.reset_row_text_if_match_keyword_exclude(config_dict, row.get("text", "")):
            continue
        candidates.append(row["index"])

    if not candidates:
        debug.log(f"[MELON {label}] no selectable row ({len(rows)} total)")
        return None

    keyword_array = _parse_keyword_array(keyword_str)
    if keyword_array:
        for order, keyword_item in enumerate(keyword_array):
            matched = [i for i in candidates if _is_match(rows[i]["text"], keyword_item)]
            if matched:
                target = util.get_target_item_from_matched_list(matched, mode)
                debug.log(f"[MELON {label}] keyword #{order + 1} {keyword_item!r} "
                          f"matched: {rows[target]['text'][:60]}")
                return target
        if not fallback:
            debug.log(f"[MELON {label}] no keyword matched and fallback is off")
            return None
        debug.log(f"[MELON {label}] no keyword matched, falling back to mode='{mode}'")

    target = util.get_target_item_from_matched_list(candidates, mode)
    debug.log(f"[MELON {label}] selected: {rows[target]['text'][:60]}")
    return target


# ===== Step 1: performance page =====

CONST_MELON_READ_PERFORMANCE_JS = '''
(function() {
    const soldOutWords = %s;

    const readRow = (li, idx) => {
        const text = (li.innerText || "").replace(/\\s+/g, " ").trim();
        const btn = li.querySelector("button");
        const disabled = !!(btn && btn.disabled) || li.classList.contains("disabled");
        const soldOut = !!li.querySelector(".sold_color")
            || soldOutWords.some(w => text.indexOf(w) >= 0);
        return {
            index: idx,
            text: text,
            selected: li.classList.contains("on"),
            disabled: disabled,
            soldOut: soldOut,
            selectable: !disabled && !soldOut && text.length > 0
        };
    };

    const readList = (sel) =>
        Array.from(document.querySelectorAll(sel)).map(readRow);

    const authPopup = document.querySelector("#authpopup");
    let authVisible = false;
    if (authPopup) {
        const st = window.getComputedStyle(authPopup);
        authVisible = st.display !== "none" && st.visibility !== "hidden";
    }

    return {
        dates: readList("#list_date > li"),
        times: readList("#list_time > li"),
        grades: Array.from(document.querySelectorAll("#list_seat > li")).map(
            (li, idx) => ({
                index: idx,
                text: (li.innerText || "").replace(/\\s+/g, " ").trim()
            })
        ),
        hasReserveBtn: !!document.querySelector("button.reservationBtn"),
        authPopupVisible: authVisible,
        waitingVisible: (function() {
            const w = document.querySelector("#pop_waiting");
            if (!w) return false;
            return window.getComputedStyle(w).display !== "none";
        })()
    };
})()
''' % json.dumps(CONST_MELON_SOLD_OUT_KEYWORDS)


CONST_MELON_CLICK_ROW_JS = '''
(function() {
    const items = document.querySelectorAll(%s);
    const li = items[%d];
    if (!li) { return { clicked: false, reason: "index-out-of-range" }; }
    const target = li.querySelector("button") || li;
    if (target.disabled) { return { clicked: false, reason: "disabled" }; }
    try { target.scrollIntoView({ behavior: "instant", block: "center" }); } catch (e) {}
    target.click();
    return { clicked: true, text: (li.innerText || "").replace(/\\s+/g, " ").trim() };
})()
'''


CONST_MELON_RESERVE_RECT_JS = '''
(function() {
    const btn = document.querySelector('button.reservationBtn');
    if (!btn) { return { ok: false, reason: 'not-found' }; }
    if (btn.disabled) { return { ok: false, reason: 'disabled' }; }

    try { btn.scrollIntoView({ behavior: 'instant', block: 'center' }); } catch (e) {}

    const r = btn.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) { return { ok: false, reason: 'zero-size' }; }

    const cx = r.x + r.width / 2;
    const cy = r.y + r.height / 2;

    // A CDP click lands on whatever is topmost at (cx, cy); an overlay would
    // silently swallow it.
    const top = document.elementFromPoint(cx, cy);
    const covered = !(top && (btn === top || btn.contains(top) || top.contains(btn)));

    return { ok: true, x: cx, y: cy, covered: covered };
})()
'''


async def _read_performance_state(tab, config_dict):
    debug = util.create_debug_logger(config_dict)
    try:
        state = util.parse_nodriver_result(
            await tab.evaluate(CONST_MELON_READ_PERFORMANCE_JS)
        )
        if isinstance(state, dict) and "dates" in state:
            return state
        debug.log(f"[MELON] unexpected page state: {type(state).__name__}")
    except Exception as exc:
        debug.log(f"[MELON] read page state failed: {exc}")
    return None


async def _click_row(tab, list_selector, index, config_dict, label):
    debug = util.create_debug_logger(config_dict)
    try:
        js = CONST_MELON_CLICK_ROW_JS % (json.dumps(list_selector), index)
        result = util.parse_nodriver_result(await tab.evaluate(js))
        if isinstance(result, dict) and result.get("clicked"):
            debug.log(f"[MELON {label}] clicked: {result.get('text', '')[:60]}")
            return True
        reason = result.get("reason", "unknown") if isinstance(result, dict) else "unknown"
        debug.log(f"[MELON {label}] click failed at index {index}: {reason}")
    except Exception as exc:
        debug.log(f"[MELON {label}] click exception: {exc}")
    return False


async def _wait_for_refresh(seconds=1.2):
    """Melon reloads the time / grade lists over ajax after each selection."""
    await asyncio.sleep(random.uniform(seconds * 0.6, seconds))


async def _click_reserve_button(tab, config_dict):
    """Click Get Tickets with a real CDP mouse event (see module docstring)."""
    debug = util.create_debug_logger(config_dict)

    rect = util.parse_nodriver_result(await tab.evaluate(CONST_MELON_RESERVE_RECT_JS))
    if not (isinstance(rect, dict) and rect.get("ok")):
        reason = rect.get("reason", "unknown") if isinstance(rect, dict) else "no-result"
        debug.log(f"[MELON RESERVE] cannot locate button: {reason}")
        return False

    # scrollIntoView needs a frame to settle before the rect is meaningful.
    await asyncio.sleep(0.25)
    rect = util.parse_nodriver_result(await tab.evaluate(CONST_MELON_RESERVE_RECT_JS))
    if not (isinstance(rect, dict) and rect.get("ok")):
        return False

    if rect.get("covered"):
        debug.log("[MELON RESERVE] button is covered by another element")
        return False

    x, y = float(rect["x"]), float(rect["y"])
    debug.log(f"[MELON RESERVE] CDP click at ({x:.0f}, {y:.0f})")
    try:
        await _cdp_click(tab, x, y)
        return True
    except Exception as exc:
        debug.log(f"[MELON RESERVE] CDP click failed: {exc}")
        return False


async def nodriver_melon_performance_main(tab, url, config_dict):
    """Step 1: date -> time -> Get Tickets."""
    if await check_and_handle_pause(config_dict):
        return

    debug = util.create_debug_logger(config_dict)
    await _register_alert_handler(tab, config_dict)

    page = await _read_performance_state(tab, config_dict)
    if page is None:
        return

    # The main loop spins every 50ms; without this the selection logs flood the
    # console and bury everything useful.
    fingerprint = (
        tuple(d.get("text", "") + str(d.get("selected")) for d in page.get("dates", [])),
        tuple(t.get("text", "") + str(t.get("selected")) for t in page.get("times", [])),
    )
    quiet = (fingerprint == _state.get("page_fingerprint"))
    _state["page_fingerprint"] = fingerprint
    pick_debug = util.create_debug_logger(enabled=False) if quiet else debug

    if page.get("waitingVisible"):
        if _throttled("log_waiting", CONST_MELON_LOG_INTERVAL):
            print("[MELON] 排隊中 (netfunnel)，等待進入訂票頁...")
        return

    if page.get("authPopupVisible"):
        if _throttled("log_auth", CONST_MELON_LOG_INTERVAL):
            print("[MELON] 偵測到會員驗證彈窗，請手動完成驗證")
        return

    dates = page.get("dates", [])
    times = page.get("times", [])

    if not dates:
        interval = max(config_dict["advanced"].get("auto_reload_page_interval", 5), 3)
        if _throttled("reload_no_date", interval):
            debug.log("[MELON DATE] no date list, reloading page")
            try:
                await tab.reload()
            except Exception:
                pass
        return

    # --- date ---
    date_index = _pick_index(
        dates,
        config_dict["date_auto_select"]["date_keyword"],
        config_dict["date_auto_select"]["mode"],
        config_dict.get("date_auto_fallback", False),
        config_dict, pick_debug, "DATE",
    )
    if date_index is None:
        return

    if not dates[date_index].get("selected"):
        if not await _click_row(tab, "#list_date > li", date_index, config_dict, "DATE"):
            return
        await _wait_for_refresh()
        page = await _read_performance_state(tab, config_dict)
        if page is None:
            return
        times = page.get("times", [])

    # --- time ---
    if not times:
        debug.log("[MELON TIME] time list not rendered yet")
        return

    # Times reuse the date keyword: users write "Sep 04" / "06:00 PM" in one
    # field and a non-matching group simply falls through. Fallback is forced
    # on -- a session always needs a time, strict mode would deadlock here.
    time_index = _pick_index(
        times,
        config_dict["date_auto_select"]["date_keyword"],
        config_dict["date_auto_select"]["mode"],
        True,
        config_dict, pick_debug, "TIME",
    )
    if time_index is None:
        return

    if not times[time_index].get("selected"):
        if not await _click_row(tab, "#list_time > li", time_index, config_dict, "TIME"):
            return
        await _wait_for_refresh()
        page = await _read_performance_state(tab, config_dict)
        if page is None:
            return

    # --- all grades sold out? reload instead of opening a dead popup ---
    grades = page.get("grades", [])
    if grades:
        all_sold = all(
            any(w in g.get("text", "") for w in CONST_MELON_SOLD_OUT_KEYWORDS)
            for g in grades
        )
        if all_sold:
            interval = max(config_dict["advanced"].get("auto_reload_page_interval", 5), 3)
            if _throttled("reload_sold_out", interval):
                debug.log("[MELON] all grades sold out, reloading")
                try:
                    await tab.reload()
                except Exception:
                    pass
            return

    # --- Get Tickets ---
    if not page.get("hasReserveBtn"):
        debug.log("[MELON] reservation button not found")
        return

    if _state.get("reserve_clicked"):
        elapsed = time.time() - _state.get("reserve_clicked_time", 0)
        if elapsed < CONST_MELON_RESERVE_RETRY_AFTER:
            if _throttled("log_waiting_popup", CONST_MELON_LOG_INTERVAL):
                print(f"[MELON] 已送出訂票請求，等待訂票視窗... ({elapsed:.0f}s)")
            return
        # Melon queues through netfunnel, so re-clicking restarts the queue.
        # Only do it once the popup has clearly failed to appear.
        debug.log(f"[MELON RESERVE] no booking window after {elapsed:.0f}s, retrying")
        _state["reserve_clicked"] = False

    if not _throttled("reserve_click", CONST_MELON_RESERVE_CLICK_INTERVAL):
        return

    if await _click_reserve_button(tab, config_dict):
        _state["reserve_clicked"] = True
        _state["reserve_clicked_time"] = time.time()
        print("[MELON] 已點擊 Get Tickets，等待訂票視窗...")
        if config_dict["advanced"]["play_sound"]["ticket"] and not _state.get("played_sound_ticket"):
            play_sound_while_ordering(config_dict)
            _state["played_sound_ticket"] = True
        send_discord_notification(config_dict, "ticket", CONST_MELON_PLATFORM_NAME)
        send_telegram_notification(config_dict, "ticket", CONST_MELON_PLATFORM_NAME)


# ===== Booking window discovery =====

async def _find_booking_tab(tab, config_dict):
    """Find the window Melon opened after Get Tickets.

    Matches by "not the page we are on" rather than a keyword allowlist: the
    original allowlist was a guess and missed the real popup entirely.
    """
    debug = util.create_debug_logger(config_dict)
    browser = getattr(tab, "browser", None)
    if browser is None:
        return None
    try:
        update_targets = getattr(browser, "update_targets", None)
        if update_targets:
            await update_targets()
    except Exception:
        pass

    current_url = str(getattr(getattr(tab, "target", None), "url", "") or "")
    seen = []
    try:
        for candidate in (getattr(browser, "tabs", None) or []):
            candidate_url = str(getattr(getattr(candidate, "target", None), "url", "") or "")
            seen.append(candidate_url)
            if not candidate_url or candidate_url.startswith("about:"):
                continue
            if candidate_url == current_url:
                continue
            if _is_melon_login_url(candidate_url):
                if _throttled("log_login_popup", CONST_MELON_LOG_INTERVAL):
                    print("[MELON] 開啟了登入視窗，請先完成登入")
                continue
            debug.log(f"[MELON] booking window found: {candidate_url}")
            return candidate
    except Exception as exc:
        debug.log(f"[MELON] window lookup failed: {exc}")

    if _throttled("log_targets", CONST_MELON_LOG_INTERVAL):
        debug.log(f"[MELON DIAG] open targets ({len(seen)}): {seen}")
    return None


# ===== iframe access =====

# onestop.htm is an empty shell. Every booking control lives in the iframe,
# which is same-origin, so contentDocument works. All booking queries must go
# through this wrapper or they search the shell and silently find nothing.
# __INNER__ is replaced textually (not %-formatted) so inner JS may contain %.
CONST_MELON_IFRAME_WRAP = '''
(function() {
    const frame = document.getElementById('oneStopFrame');
    if (!frame) { return { ok: false, reason: 'no-iframe' }; }
    let doc = null;
    try {
        doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
    } catch (e) {
        return { ok: false, reason: 'cross-origin: ' + e };
    }
    if (!doc || !doc.body) { return { ok: false, reason: 'no-document' }; }
    if (doc.readyState === 'loading') { return { ok: false, reason: 'loading' }; }
    try {
        return { ok: true, data: (function(document) { __INNER__ })(doc) };
    } catch (e) {
        return { ok: false, reason: 'inner-threw: ' + e };
    }
})()
'''


async def _eval_in_frame(tab, inner_js, config_dict, label):
    """Run inner_js with `document` bound to the booking iframe."""
    debug = util.create_debug_logger(config_dict)
    try:
        wrapped = CONST_MELON_IFRAME_WRAP.replace("__INNER__", inner_js)
        result = util.parse_nodriver_result(await tab.evaluate(wrapped))
        if isinstance(result, dict) and result.get("ok"):
            return result.get("data")
        reason = result.get("reason", "unknown") if isinstance(result, dict) else "no-result"
        if _throttled(f"frame_fail_{label}", CONST_MELON_LOG_INTERVAL):
            debug.log(f"[MELON {label}] iframe not ready: {reason}")
    except Exception as exc:
        debug.log(f"[MELON {label}] iframe eval failed: {exc}")
    return None


async def _frame_url(tab, config_dict):
    url = await _eval_in_frame(tab, "return document.location.href;", config_dict, "URL")
    return url if isinstance(url, str) else ""


def _frame_step(frame_url):
    """Which booking step the iframe is on ('' means not loaded yet).

    Path names are Melon's own abbreviations taken from live URLs: the delivery
    step is stepDelvy.htm, not stepDelivery. Names are normalised so the
    dispatcher and the submit-gate cleanup agree on one vocabulary.
    """
    low = (frame_url or "").lower()
    if not low or not low.startswith("http"):
        return ""
    table = [
        ("stepticket", "stepticket"),
        ("stepdelvy", "stepdelivery"),      # observed live
        ("stepdelivery", "stepdelivery"),   # kept in case Melon renames it
        ("steppay", "stepdelivery"),
        ("stepseat", "stepseat"),
        ("stepblock", "stepblock"),
        ("stepfinish", "stepfinish"),
        ("pick.htm", "pick"),
    ]
    for needle, name in table:
        if needle in low:
            return name
    return "unknown"


# ===== Frame diagnostics =====

CONST_MELON_FRAME_PROBE_JS = '''
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    // Melon labels controls three different ways: a wrapping <label>, a
    // label[for], or plain sibling text. Collect all of them or a text match
    // against the label will miss.
    const labelOf = (el) => {
        let t = "";
        const wrap = el.closest('label');
        if (wrap) t += " " + (wrap.innerText || "");
        if (el.id) {
            const lf = document.querySelector('label[for="' + el.id + '"]');
            if (lf) t += " " + (lf.innerText || "");
        }
        const p = el.parentElement;
        if (p) t += " " + (p.innerText || "");
        return t.replace(/\\s+/g, ' ').trim().slice(0, 60);
    };
    const ctl = (el) => ({
        id: el.id, name: el.name, value: el.value,
        checked: !!el.checked, disabled: !!el.disabled, label: labelOf(el)
    });
    return {
        url: document.location.href,
        selects: Array.from(document.querySelectorAll('select')).map(s => ({
            id: s.id, name: s.name, disabled: s.disabled, value: s.value,
            options: Array.from(s.options).slice(0, 20)
                .map(o => o.value + '|' + (o.text || '').trim())
        })),
        radios: Array.from(document.querySelectorAll('input[type=radio]'))
            .filter(visible).map(ctl),
        checkboxes: Array.from(document.querySelectorAll('input[type=checkbox]'))
            .filter(visible).map(ctl),
        buttons: Array.from(document.querySelectorAll(
            'button, a, input[type=button], input[type=submit]'
        )).filter(visible).map(b => ({
            tag: b.tagName, id: b.id, cls: b.className,
            text: (b.innerText || b.value || '').replace(/\\s+/g, ' ').trim().slice(0, 40)
        })),
        textInputs: Array.from(document.querySelectorAll(
            'input[type=number], input[type=text], input[type=tel]'
        )).filter(visible).map(i => ({
            id: i.id, name: i.name, cls: i.className,
            value: i.value, ph: i.placeholder || '', label: labelOf(i)
        }))
    };
'''


async def _probe_booking_frame(tab, config_dict, step):
    """Structural dump of the current step, once per step.

    Gated on a real http URL: about:blank reports readyState 'complete', so a
    readiness check alone would latch this onto an empty document forever.
    """
    if not step:
        return
    probed = _state.setdefault("probed_steps", set())
    if step in probed:
        return
    info = await _eval_in_frame(tab, CONST_MELON_FRAME_PROBE_JS, config_dict, "PROBE")
    if not isinstance(info, dict) or not str(info.get("url", "")).startswith("http"):
        return
    probed.add(step)
    debug = util.create_debug_logger(config_dict)
    debug.log(f"[MELON PROBE:{step}] url={info.get('url')}")
    debug.log(f"[MELON PROBE:{step}] selects={info.get('selects')}")
    debug.log(f"[MELON PROBE:{step}] radios={info.get('radios')}")
    debug.log(f"[MELON PROBE:{step}] checkboxes={info.get('checkboxes')}")
    debug.log(f"[MELON PROBE:{step}] textInputs={info.get('textInputs')}")
    debug.log(f"[MELON PROBE:{step}] buttons={info.get('buttons')}")


async def _dump_booking_dom_once(tab, config_dict, step):
    """Save each step's frame HTML so selectors can be checked against reality."""
    debug = util.create_debug_logger(config_dict)
    if not debug.enabled or not step:
        return
    dumped = _state.setdefault("dumped_steps", set())
    if step in dumped:
        return
    html = await _eval_in_frame(
        tab, "return document.documentElement.outerHTML;", config_dict, "DUMP"
    )
    if not html or len(html) < 512:
        return          # still loading; try again next round
    dumped.add(step)
    try:
        dump_dir = os.path.join(util.get_app_root(), ".temp", "platform", "melon")
        os.makedirs(dump_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(dump_dir, f"{step}_{stamp}.html")
        with open(path, "w", encoding="utf-8") as dump_file:
            dump_file.write(html)
        debug.log(f"[MELON] {step} DOM dumped: {path}")
    except Exception as exc:
        debug.log(f"[MELON] DOM dump failed: {exc}")


# ===== Shared button helpers =====

CONST_MELON_FIND_BUTTON_JS = '''
    const wanted = %s;
    const cands = Array.from(document.querySelectorAll(
        'button, a, input[type=button], input[type=submit]'
    ));
    for (const el of cands) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (el.disabled) continue;
        const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
        if (!wanted.some(w => text.toLowerCase().indexOf(w.toLowerCase()) >= 0)) continue;
        return { found: true, text: text, id: el.id, cls: el.className };
    }
    return { found: false, count: cands.length };
'''


CONST_MELON_CLICK_BUTTON_JS = '''
    const wanted = %s;
    for (const el of document.querySelectorAll(
        'button, a, input[type=button], input[type=submit]'
    )) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0 || el.disabled) continue;
        const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
        if (!wanted.some(w => text.toLowerCase().indexOf(w.toLowerCase()) >= 0)) continue;
        try { el.scrollIntoView({ behavior: 'instant', block: 'center' }); } catch (e) {}
        el.click();
        return { clicked: true, text: text };
    }
    return { clicked: false };
'''


async def _melon_click_labeled_button(tab, config_dict, texts, label):
    """Find then click an in-frame button by its visible text.

    An in-frame click suffices: these buttons submit a form rather than opening
    a window, so no user activation is required (unlike Get Tickets).
    """
    debug = util.create_debug_logger(config_dict)
    found = await _eval_in_frame(
        tab, CONST_MELON_FIND_BUTTON_JS % json.dumps(texts), config_dict, label
    )
    if not (isinstance(found, dict) and found.get("found")):
        if _throttled(f"log_no_{label}", CONST_MELON_LOG_INTERVAL):
            count = found.get("count", "?") if isinstance(found, dict) else "?"
            print(f"[MELON] 尚未找到 {label} 按鈕（掃描 {count} 個可點元素）")
        return False

    debug.log(f"[MELON {label}] found: {found.get('text')!r} id={found.get('id')!r}")
    clicked = await _eval_in_frame(
        tab, CONST_MELON_CLICK_BUTTON_JS % json.dumps(texts), config_dict, label
    )
    return isinstance(clicked, dict) and bool(clicked.get("clicked"))


def _submit_once(key, stuck_message):
    """Gate a form submission so the 50ms main loop cannot repeat it.

    Returns one of:
      'go'    -- no submission in flight, the caller may click
      'wait'  -- a submission is in flight, do nothing
      'stuck' -- the cooldown expired and we are still here; the caller should
                 report it and stop rather than resubmit

    This exists because the iframe keeps its pre-submit URL until the POST
    completes. Without it the module pressed Next ~5 times a second and Melon
    answered with 403.
    """
    sent_at = _state.get(key, 0)
    if not sent_at:
        return "go"
    elapsed = time.time() - sent_at
    if elapsed < CONST_MELON_SUBMIT_COOLDOWN:
        if _throttled(f"log_wait_{key}", CONST_MELON_LOG_INTERVAL):
            print(f"[MELON] {stuck_message}... ({elapsed:.0f}s)")
        return "wait"
    return "stuck"


def _report_stuck(key, what):
    """Explain a submission that never navigated, without resubmitting it."""
    if not _throttled(f"log_stuck_{key}", CONST_MELON_STUCK_LOG_INTERVAL):
        return
    last = _state.get("last_alert", "")
    suffix = f"，最後訊息：{last}" if last else ""
    print(f"[MELON] 送出後仍停留在{what}{suffix}")
    print("[MELON] 請檢查瀏覽器畫面；若被伺服器擋下（403）請關閉程式稍候再試")


# ===== stepTicket: quantity + Next =====

CONST_MELON_SET_QTY_JS = '''
    const want = String(%d);
    const requested = Number(want);
    const selects = Array.from(document.querySelectorAll('select'))
        .filter(s => !s.disabled && s.offsetParent !== null);

    function setQuantity(sel, opt, selectedCount, fallback) {
        if (sel.value === opt.value) {
            return { success: true, already: true, id: sel.id, name: sel.name,
                     selectedCount: selectedCount, fallback: fallback };
        }
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return { success: true, already: false, id: sel.id, name: sel.name,
                 value: opt.value, selectedCount: selectedCount,
                 fallback: fallback };
    }

    // Prefer the requested count exactly.  Melon exposes one select per ticket
    // type, so checking every select preserves the existing ticket-type choice.
    for (const sel of selects) {
        const opt = Array.from(sel.options)
            .find(o => o.value === want || (o.text || '').trim() === want);
        if (opt) return setQuantity(sel, opt, requested, false);
    }

    // If the requested quantity is sold out, use the largest remaining option
    // that is still no greater than the request (e.g. 4 -> 3 -> 2 -> 1).
    let fallback = null;
    for (const sel of selects) {
        for (const opt of Array.from(sel.options)) {
            const value = (opt.value || '').trim();
            const text = (opt.text || '').trim();
            const valueCount = Number(value);
            const textCount = Number(text);
            const count = Number.isInteger(valueCount) ? valueCount : textCount;
            if (!Number.isInteger(count) || count < 1 || count > requested) continue;
            if (!fallback || count > fallback.count) {
                fallback = { sel: sel, opt: opt, count: count };
            }
        }
    }
    if (fallback) {
        return setQuantity(fallback.sel, fallback.opt, fallback.count, true);
    }
    return { success: false, selectCount: selects.length,
             optionSets: selects.map(s => Array.from(s.options)
                 .map(o => o.value).slice(0, 10)) };
'''


async def _melon_ticket_step(tab, config_dict):
    """stepTicket: set the ticket count, then press Next exactly once."""
    debug = util.create_debug_logger(config_dict)

    gate = _submit_once("ticket_submit_time", "已送出張數，等待進入付款頁")
    if gate == "wait":
        return
    if gate == "stuck":
        _report_stuck("ticket_submit_time", "選張數頁")
        return

    ticket_number = int(config_dict.get("ticket_number", 1))

    qty = await _eval_in_frame(
        tab, CONST_MELON_SET_QTY_JS % ticket_number, config_dict, "QTY"
    )
    if not (isinstance(qty, dict) and qty.get("success")):
        if _throttled("log_no_qty", CONST_MELON_LOG_INTERVAL):
            count = qty.get("selectCount", "?") if isinstance(qty, dict) else "?"
            print(f"[MELON] 尚未找到可選 {ticket_number} 張的下拉選單"
                  f"（頁面上有 {count} 個 select）")
            if isinstance(qty, dict) and qty.get("optionSets"):
                debug.log(f"[MELON QTY] available options: {qty['optionSets']}")
        return

    selected_number = qty.get("selectedCount", ticket_number)
    if not qty.get("already"):
        selection_note = " (requested unavailable)" if qty.get("fallback") else ""
        debug.log(f"[MELON QTY] set {selected_number} on select "
                  f"{selection_note}"
                  f"id={qty.get('id')!r} name={qty.get('name')!r}")
        await asyncio.sleep(0.4)   # let the page recalculate the total

    if await _melon_click_labeled_button(tab, config_dict,
                                         CONST_MELON_NEXT_TEXTS, "NEXT"):
        _state["ticket_submit_time"] = time.time()
        if qty.get("fallback"):
            print(f"[MELON] 要求 {ticket_number} 張但庫存不足，改選 {selected_number} 張並點擊 Next")
        else:
            print(f"[MELON] 已選 {selected_number} 張並點擊 Next")


# ===== stepDelivery / stepPay: mobile, card, terms, checkout =====

CONST_MELON_DELIVERY_JS = '''
    const phone = %s;
    const report = { phone: 'skip', payment: 'miss', card: 'miss',
                     agreed: 0, checkboxTotal: 0, unchecked: [] };

    // --- mobile number ---
    const tel = document.getElementById('tel');
    if (!tel) {
        report.phone = 'no-field';
    } else if (tel.value && tel.value.trim()) {
        report.phone = 'already:' + tel.value;
    } else if (phone) {
        tel.focus();
        tel.value = phone;
        tel.dispatchEvent(new Event('input', { bubbles: true }));
        tel.dispatchEvent(new Event('change', { bubbles: true }));
        tel.dispatchEvent(new Event('blur', { bubbles: true }));
        report.phone = 'set';
    }

    // --- payment method: International Credit Card ---
    const pay = document.getElementById('payMethodCode001');
    if (pay && !pay.disabled) {
        if (pay.checked) {
            report.payment = 'already';
        } else {
            pay.click();
            report.payment = 'clicked';
        }
    }

    // --- card brand: the VISA option of #cardCode ---
    // The select only renders after a payment method is chosen, so this runs
    // after the click above rather than in its own pass.
    const card = document.getElementById('cardCode');
    if (card && !card.disabled) {
        if (card.value === 'FOREIGN_VISA') {
            report.card = 'already';
        } else {
            card.value = 'FOREIGN_VISA';
            card.dispatchEvent(new Event('change', { bubbles: true }));
            report.card = card.value === 'FOREIGN_VISA' ? 'set' : 'reject';
        }
    }

    // --- terms ---
    // #chkAgreeAll is supposed to cascade to the four chkAgree boxes, but do
    // not trust it: sweep whatever it left unchecked before reporting.
    const master = document.getElementById('chkAgreeAll');
    if (master && !master.checked) master.click();
    const boxes = Array.from(document.querySelectorAll('input[name="chkAgree"]'))
        .filter(c => !c.disabled);
    for (const c of boxes) { if (!c.checked) c.click(); }
    report.checkboxTotal = boxes.length;
    report.agreed = boxes.filter(c => c.checked).length;
    report.unchecked = boxes.filter(c => !c.checked).map(c => c.id || '?');

    return report;
'''


async def _melon_delivery_step(tab, config_dict):
    """Fill mobile, pick International Credit Card + VISA, agree to terms.

    Returns True only when the form is complete: each of these is required by
    Melon, and submitting without them just bounces off a validation alert.
    """
    debug = util.create_debug_logger(config_dict)
    phone = CONST_MELON_PHONE.strip()

    report = await _eval_in_frame(
        tab, CONST_MELON_DELIVERY_JS % json.dumps(phone), config_dict, "DELIVERY"
    )

    if not isinstance(report, dict):
        return False

    # Log once per distinct outcome, not once per 50ms poll.
    signature = (report.get("phone"), report.get("payment"), report.get("card"),
                 report.get("agreed"), report.get("checkboxTotal"))
    if signature != _state.get("delivery_signature"):
        _state["delivery_signature"] = signature
        debug.log(f"[MELON DELIVERY] {report}")

    problems = []
    if not phone:
        problems.append("melon.py 的 CONST_MELON_PHONE 是空的，請直接在程式碼填入手機號碼")
    elif report.get("phone") == "no-field":
        problems.append("找不到手機號碼欄位 (#tel)")
    if report.get("payment") == "miss":
        problems.append("找不到 International Credit Card 選項")
    if report.get("card") == "miss":
        problems.append("找不到 VISA 選項")
    if report.get("unchecked"):
        problems.append(f"仍有未勾選的同意項：{report['unchecked']}")

    if problems:
        if _throttled("log_delivery_problem", CONST_MELON_LOG_INTERVAL):
            for item in problems:
                print(f"[MELON] {item}")
        return False

    if not _state.get("delivery_ready_logged"):
        _state["delivery_ready_logged"] = True
        print("[MELON] 表單已填妥：手機 / 海外信用卡 / VISA / 同意條款")

    await asyncio.sleep(0.4)   # let the form revalidate before submitting
    return True


async def _melon_checkout_step(tab, config_dict):
    """Press Checkout exactly once, then hand the payment gateway to the user."""
    gate = _submit_once("checkout_submit_time", "已送出結帳，等待付款頁")
    if gate == "wait":
        return False
    if gate == "stuck":
        _report_stuck("checkout_submit_time", "付款資料頁")
        return False

    if not await _melon_click_labeled_button(tab, config_dict,
                                             CONST_MELON_CHECKOUT_TEXTS, "CHECKOUT"):
        return False

    _state["checkout_submit_time"] = time.time()
    return True


# ===== Booking dispatcher =====

async def nodriver_melon_booking_main(tab, url, config_dict):
    """Booking popup: quantity -> Next -> delivery form -> Checkout."""
    if await check_and_handle_pause(config_dict):
        return
    if _state.get("manual_takeover"):
        return

    debug = util.create_debug_logger(config_dict)
    await _register_alert_handler(tab, config_dict)

    frame_url = await _frame_url(tab, config_dict)
    step = _frame_step(frame_url)
    if not step:
        return          # iframe still about:blank

    if step != _state.get("last_step"):
        previous = _state.get("last_step")
        _state["last_step"] = step
        debug.log(f"[MELON STEP] {previous or 'start'} -> {step} ({frame_url})")
        # Leaving a step means its submission landed; clear the gate so a
        # Previous/back navigation can retry cleanly.
        if step != "stepticket":
            _state.pop("ticket_submit_time", None)
        if step != "stepdelivery":
            _state.pop("checkout_submit_time", None)
            _state.pop("delivery_ready_logged", None)
            _state.pop("delivery_signature", None)

    await _dump_booking_dom_once(tab, config_dict, step)
    await _probe_booking_frame(tab, config_dict, step)

    if step in ("stepseat", "stepblock", "pick"):
        if _throttled("log_seat_step", CONST_MELON_LOG_INTERVAL):
            print(f"[MELON] 此活動需要選位（{step}），尚未自動化，請手動選位")
        return

    if step == "stepfinish":
        _state["manual_takeover"] = True
        print("=" * 60)
        print("[MELON] 已進入完成頁，訂票流程結束")
        print("=" * 60)
        if config_dict["advanced"]["play_sound"]["order"]:
            play_sound_while_ordering(config_dict)
        send_discord_notification(config_dict, "order", CONST_MELON_PLATFORM_NAME)
        send_telegram_notification(config_dict, "order", CONST_MELON_PLATFORM_NAME)
        return

    if step == "stepticket":
        await _melon_ticket_step(tab, config_dict)
        return

    if step == "unknown":
        if _throttled("log_unknown_step", CONST_MELON_LOG_INTERVAL):
            debug.log(f"[MELON] unrecognised booking step: {frame_url}")
        return

    # --- stepDelivery / stepPay ---
    if not await _melon_delivery_step(tab, config_dict):
        return

    if not CONST_MELON_AUTO_CHECKOUT:
        if _throttled("log_checkout_off", CONST_MELON_LOG_INTERVAL):
            print("[MELON] CONST_MELON_AUTO_CHECKOUT 為 False，請自行按 Checkout")
        return

    if not await _melon_checkout_step(tab, config_dict):
        return

    _state["manual_takeover"] = True
    print("=" * 60)
    print("[MELON] 已點擊 Checkout")
    print("[MELON] 請在後續的信用卡頁面完成 3-D Secure 驗證與付款")
    print("=" * 60)
    if config_dict["advanced"]["play_sound"]["order"]:
        play_sound_while_ordering(config_dict)
    send_discord_notification(config_dict, "order", CONST_MELON_PLATFORM_NAME)
    send_telegram_notification(config_dict, "order", CONST_MELON_PLATFORM_NAME)


# ===== Entry point =====

async def nodriver_melon_main(tab, url, config_dict):
    """Dispatcher. Returns the tab to keep polling (may switch to the popup)."""
    debug = util.create_debug_logger(config_dict)

    if _is_melon_login_url(url):
        await _wait_for_manual_login(tab)
        return tab

    if is_melon_booking_url(url):
        await nodriver_melon_booking_main(tab, url, config_dict)
        return tab

    if is_melon_performance_url(url):
        _reset_state_for_product(url)

        # A booking window opened by an earlier click takes priority.
        booking_tab = await _find_booking_tab(tab, config_dict)
        if booking_tab is not None:
            try:
                await booking_tab.activate()
            except Exception:
                pass
            debug.log("[MELON] switching main loop to the booking window")
            return booking_tab

        await nodriver_melon_performance_main(tab, url, config_dict)
        return tab

    return tab
