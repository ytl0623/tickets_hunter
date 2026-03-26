"""
竹北羽球場自動搶位機器人 v3 — nodriver 版
目標: https://fe.xuanen.com.tw (宣恩場館系統)

安裝:
    pip install nodriver

執行:
    python zhubei_badminton_bot.py
    python zhubei_badminton_bot.py --date 2026/04/10 --session 1 --times "09:00~10:00,10:00~11:00"
"""

import asyncio
import re
import sys
import logging
from datetime import datetime

import nodriver as uc

# ─────────────────────────────────────────────
#  ★ 設定區 ★
# ─────────────────────────────────────────────
CONFIG = {
    # 預約日期
    "target_date": "2026/04/02",

    # 場次 D2: 1=早上(06~12) / 2=下午(12~18) / 3=晚上(18~22)
    "session": "1",

    # 目標時段（優先序，格式需與網站一致）
    "target_times": [
        "09:00~10:00",
        "20:00~21:00",
    ],

    # 偏好場地（空 = 任意）; 名稱需與網站完全一致，例: "2F羽 1", "4F羽B2"
    "prefer_courts": [],

    # 搶票行為
    "refresh_interval": 0.1,    # 每次刷新間隔（秒）
    "max_retry":        999,    # 最大重試次數
    "booking_open_time": None,  # 開放預約時間 "2026/04/07 08:00:00"，None=立即

    # 找到多個符合時段時，是否只搶第一個就停
    "stop_on_first": True,
}

SITE = {
    "login_url":   "https://fe.xuanen.com.tw/fe02.aspx?module=login_people&files=login",
    "booking_url": (
        "https://fe.xuanen.com.tw/fe02.aspx"
        "?module=net_booking&files=booking_place"
        "&StepFlag=2&PT={PT}&D={D}&D2={D2}"
    ),
}

# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("booking_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"
def cp(c, m): print(f"{c}{m}{X}", flush=True)

# ─────────────────────────────────────────────
#  輔助函式
# ─────────────────────────────────────────────
def build_url(cfg: dict) -> str:
    return SITE["booking_url"].format(
        PT=cfg["facility_type"] if "facility_type" in cfg else "1",
        D=cfg["target_date"],
        D2=cfg["session"],
    )

def qtime_to_str(h: int) -> str:
    """6 → '06:00~07:00'"""
    return f"{h:02d}:00~{h+1:02d}:00"

async def ainput(prompt: str = "") -> str:
    """非同步 input"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))

async def wait_open(open_str: str | None) -> None:
    if not open_str:
        return
    target = datetime.strptime(open_str, "%Y/%m/%d %H:%M:%S")
    cp(C, f"\n⏰ 預約開放時間：{open_str}")
    while True:
        diff = (target - datetime.now()).total_seconds()
        if diff <= 1:
            cp(G, "🚀 時間到！開始搶位！")
            break
        if diff > 60:
            cp(Y, f"  ⏳ 距離開放還有 {diff:.0f} 秒，等待中…")
            await asyncio.sleep(min(diff - 5, 30))
        else:
            sys.stdout.write(f"\r  ⏳ 倒數 {diff:6.1f} 秒…  ")
            sys.stdout.flush()
            await asyncio.sleep(0.5)
    print()

def play_sound() -> None:
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 500)
    except Exception:
        try:
            import os
            os.system(
                "afplay /System/Library/Sounds/Glass.aiff 2>/dev/null || "
                "paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null"
            )
        except Exception:
            print("\a\a\a")

# ─────────────────────────────────────────────
#  JavaScript: 掃描可用時段
#  回傳格式: [{qpid, qtime, courtName, timeStr}, ...]
# ─────────────────────────────────────────────
JS_GET_SLOTS = r"""
(function() {
    const btns = document.querySelectorAll('img[name="PlaceBtn"]');
    const result = [];
    for (const btn of btns) {
        const oc = btn.getAttribute('onclick') || '';
        const m  = oc.match(/Step3Action\((\d+),(\d+)\)/);
        if (!m) continue;
        const qpid  = parseInt(m[1]);
        const qtime = parseInt(m[2]);

        // timeStr 直接由 qtime 數字推算，不依賴 DOM
        const h       = qtime;
        const timeStr = String(h).padStart(2,'0') + ':00~'
                      + String(h+1).padStart(2,'0') + ':00';

        // 解析場地名稱：
        //   有 rowspan 的首行 → 4 個 td：[時間, 場地, 費用, 操作]
        //   非首行           → 3 個 td：[場地, 費用, 操作]
        const row  = btn.closest('tr');
        const tds  = row ? [...row.querySelectorAll('td')] : [];
        let courtName = '?';
        if (tds.length >= 4)      courtName = tds[1].textContent.trim(); // 首行
        else if (tds.length >= 3) courtName = tds[0].textContent.trim(); // 非首行
        else if (tds.length >= 1) courtName = tds[0].textContent.trim();

        result.push({ qpid, qtime, courtName, timeStr });
    }
    // 用 JSON.stringify 確保 nodriver evaluate 完整回傳所有 key
    return JSON.stringify(result);
})();
"""

# ─────────────────────────────────────────────
#  JavaScript: 等待 Turnstile Cookie 就緒
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  JavaScript: 偵測頁面是否「尚未開放」
#  回傳 {notOpen: bool, reason: string}
# ─────────────────────────────────────────────
JS_CHECK_OPEN = r"""
(function() {
    const body = document.body ? document.body.innerText : '';

    // 明確的未開放關鍵字
    const NOT_OPEN_KEYWORDS = [
        '尚未開放', '尚未到預約時間', '預約時間未到',
        '不在預約時段', '非預約時間', '目前無法預約',
        '開放時間', '請於', '暫停預約',
    ];
    for (const kw of NOT_OPEN_KEYWORDS) {
        if (body.includes(kw)) return JSON.stringify({ notOpen: true, reason: kw });
    }

    // 有預約按鈕 = 已開放
    const hasBtn = document.querySelectorAll('img[name="PlaceBtn"]').length > 0;
    if (hasBtn) return JSON.stringify({ notOpen: false, reason: 'hasBtn' });

    // 有「已被預約」圖示但沒有可用按鈕 = 頁面已載入但全滿（非未開放）
    const hasFullImg = document.querySelectorAll('img[title="已被預約"]').length > 0;
    if (hasFullImg) return JSON.stringify({ notOpen: false, reason: 'allFull' });

    // 頁面有日期選單代表正常載入但當天全滿或尚無資料
    const hasDateSel = !!document.querySelector('select[name="years"]');
    if (hasDateSel) return JSON.stringify({ notOpen: false, reason: 'noSlots' });

    // 其他：視為未開放（頁面可能未完整載入）
    return JSON.stringify({ notOpen: true, reason: 'unknown' });
})();
"""

JS_HAS_TOKEN = r"""
(function() {
    return document.cookie.split('; ')
        .some(r => r.startsWith('captchaToken=') && r.split('=')[1].length > 10);
})();
"""

# ─────────────────────────────────────────────
#  核心流程
# ─────────────────────────────────────────────
async def get_slots(page) -> list[dict]:
    try:
        raw = await page.evaluate(JS_GET_SLOTS)
        # nodriver 回傳 JSON 字串 → 解析為 list[dict]
        if isinstance(raw, str):
            import json
            slots = json.loads(raw)
        elif isinstance(raw, list):
            slots = raw
        else:
            log.debug(f"get_slots 非預期回傳型別: {type(raw)} | {raw}")
            return []

        # 防禦：過濾掉缺少必要 key 的 item
        valid = [s for s in slots if all(k in s for k in ("qpid","qtime","courtName","timeStr"))]
        if len(valid) != len(slots):
            log.debug(f"過濾掉 {len(slots)-len(valid)} 個不完整 slot")
        return valid
    except Exception as e:
        log.debug(f"get_slots error: {e}")
        return []

def slot_matches(slot: dict, cfg: dict) -> bool:
    if slot["timeStr"] not in cfg["target_times"]:
        return False
    if cfg["prefer_courts"]:
        return any(c in slot["courtName"] for c in cfg["prefer_courts"])
    return True

async def wait_turnstile(page, timeout: float = 8.0) -> bool:
    """等待 Cloudflare Turnstile 完成並寫入 Cookie"""
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        has = await page.evaluate(JS_HAS_TOKEN)
        if has:
            return True
        await asyncio.sleep(0.3)
    log.warning("Turnstile token 等待逾時，嘗試繼續…")
    return False

async def book_slot(page, slot: dict, cfg: dict) -> bool:
    """執行預約動作，成功回傳 True"""
    qpid, qtime = slot["qpid"], slot["qtime"]
    court, ts   = slot["courtName"], slot["timeStr"]
    cp(G, f"\n🎯 嘗試預約：{court}  {ts}  (QPid={qpid}, QTime={qtime})")

    # 等 Turnstile token（Cloudflare 人機驗證，真實瀏覽器通常自動通過）
    cp(C, "  ⌛ 等待 Turnstile 驗證…")
    token_ready = await wait_turnstile(page)
    if not token_ready:
        cp(Y, "  ⚠️  Token 未就緒，仍嘗試送出")

    # 覆蓋 confirm → 自動同意；覆蓋 alert → 靜默
    await page.evaluate("""
        window.confirm = () => true;
        window.alert   = () => {};
    """)

    # 呼叫 Step3Action（等同點選「場地預定」按鈕）
    await page.evaluate(f"Step3Action({qpid}, {qtime});")

    # 等待頁面跳轉（最多 8 秒）
    for _ in range(16):
        await asyncio.sleep(0.5)
        try:
            cur_url = page.url
        except Exception:
            cur_url = ""
        if "StepFlag=25" in cur_url or "StepFlag=3" in cur_url:
            cp(G, f"  ✅ 頁面跳轉至 {cur_url}")
            return True

    # 跳轉失敗：讀頁面文字判斷結果
    try:
        body = await page.evaluate("document.body.innerText || ''")
    except Exception:
        body = ""

    if any(k in body for k in ("成功", "預約完成", "確認單")):
        cp(G, "  ✅ 頁面顯示成功訊息")
        return True
    if any(k in body for k in ("已被預約", "失敗", "驗證失敗", "error")):
        cp(R, f"  ❌ 預約失敗：{body[:80]}")
        return False

    cp(Y, "  ⚠️  無法確定結果，請確認瀏覽器頁面")
    return False  # 當做失敗，繼續搶下一個

# ─────────────────────────────────────────────
#  手動登入流程
# ─────────────────────────────────────────────
async def manual_login(browser) -> tuple[bool, object]:
    """開啟登入頁，等使用者手動登入，驗證後回傳 (True, page)"""
    cp(C, "\n" + "═"*55)
    cp(C,  "  🌐 開啟登入頁，請在瀏覽器中完成登入")
    cp(C,  "═"*55)

    page = await browser.get(SITE["login_url"])
    await asyncio.sleep(2)

    while True:
        cp(Y, "\n登入完成後，回到此視窗按 Enter 確認（輸入 q 放棄）：")
        ans = await ainput()
        if ans.strip().lower() == "q":
            return False, None

        try:
            cur_url = page.url
            src     = await page.evaluate("document.body.innerHTML")
        except Exception as e:
            cp(R, f"無法讀取頁面：{e}"); continue

        on_login  = "login" in cur_url.lower()
        has_pwd   = bool(re.search(r'type=["\']password["\']', src, re.I))

        if not on_login or not has_pwd:
            cp(G, "\n✅ 登入驗證通過！Bot 準備搶位…")
            log.info(f"登入成功 | URL: {cur_url}")
            return True, page

        cp(R, "\n❌ 偵測到仍在登入頁或仍有密碼欄位，請確認後再按 Enter")

# ─────────────────────────────────────────────
#  主程式
# ─────────────────────────────────────────────
async def run(cfg: dict) -> None:
    cp(B+C, "\n" + "═"*55)
    cp(B+C,  "  🏸 竹北羽球場搶位機器人 v3 (nodriver)")
    cp(C,   f"  日期：{cfg['target_date']}  場次 D2={cfg['session']}")
    cp(C,   f"  目標時段：{', '.join(cfg['target_times'])}")
    cp(C,   f"  偏好場地：{', '.join(cfg['prefer_courts']) or '任意'}")
    if cfg.get("booking_open_time"):
        cp(C, f"  開放時間：{cfg['booking_open_time']}")
    cp(B+C,  "═"*55)

    browser = await uc.start(headless=False)

    try:
        # ── 1. 手動登入 ───────────────────────
        ok, page = await manual_login(browser)
        if not ok:
            cp(R, "⛔ 登入中止"); return

        # ── 2. 等待開放時間 ───────────────────
        await wait_open(cfg.get("booking_open_time"))

        # ── 3. 前往預約頁 ─────────────────────
        burl = build_url(cfg)
        cp(C, f"\n🌐 前往預約頁：{burl}")
        page = await browser.get(burl)
        await asyncio.sleep(2)

        # ── 4. 搶位主迴圈 ─────────────────────
        not_open_streak = 0   # 連續偵測到「未開放」的次數

        for retry in range(1, cfg["max_retry"] + 1):

            # Session 過期偵測
            try:
                cur_url = page.url
                if "login" in cur_url.lower():
                    cp(R, "\n🔒 Session 過期！請重新登入後按 Enter…")
                    await ainput()
                    page = await browser.get(burl)
                    await asyncio.sleep(2)
                    continue
            except Exception:
                pass

            # 刷新（第一次不刷）
            if retry > 1:
                try:
                    await page.reload()
                    await asyncio.sleep(0.5)   # 等頁面基本渲染
                except Exception as e:
                    log.debug(f"reload error: {e}")
                    page = await browser.get(burl)
                    await asyncio.sleep(2)

            # ── 未開放偵測 ───────────────────
            try:
                import json as _json
                raw_open = await page.evaluate(JS_CHECK_OPEN)
                open_info = _json.loads(raw_open) if isinstance(raw_open, str) else {"notOpen": True, "reason": "evalFail"}
            except Exception as e:
                log.debug(f"check_open error: {e}")
                open_info = {"notOpen": True, "reason": "evalFail"}

            if open_info.get("notOpen"):
                not_open_streak += 1
                reason = open_info.get("reason", "?")

                # 決定等待間隔：前 3 次快速確認，之後依距開放時間遠近調整
                if not_open_streak <= 3:
                    wait_sec = 2.0
                elif cfg.get("booking_open_time"):
                    diff = (datetime.strptime(cfg["booking_open_time"], "%Y/%m/%d %H:%M:%S")
                            - datetime.now()).total_seconds()
                    if diff > 3600:
                        wait_sec = 300   # 超過 1 小時：每 5 分鐘確認
                    elif diff > 300:
                        wait_sec = 30    # 5~60 分鐘：每 30 秒確認
                    elif diff > 60:
                        wait_sec = 5     # 1~5 分鐘：每 5 秒確認
                    else:
                        wait_sec = 1     # 最後 1 分鐘：每秒衝
                else:
                    wait_sec = 0.1        # 沒設開放時間：每 10 秒確認

                sys.stdout.write(
                    f"\r⏳ 第 {retry:4d} 次｜頁面尚未開放（{reason}）"
                    f"｜{wait_sec:.0f}s 後重試…  "
                )
                sys.stdout.flush()
                await asyncio.sleep(wait_sec)
                continue

            # 頁面已開放，重置連續計數
            if not_open_streak > 0:
                print()
                cp(G, f"🔓 頁面已開放！（曾等待 {not_open_streak} 次）開始搶位…")
                not_open_streak = 0

            sys.stdout.write(f"\r🔄 第 {retry:4d} 次掃描…  ")
            sys.stdout.flush()

            # 掃描可用時段
            slots   = await get_slots(page)
            matched = [s for s in slots if slot_matches(s, cfg)]

            if not matched:
                reason_str = open_info.get("reason", "")
                if reason_str == "allFull":
                    sys.stdout.write(f"\r😞 第 {retry:4d} 次｜全部時段已滿，{cfg['refresh_interval']}s 後重試…  ")
                    sys.stdout.flush()
                await asyncio.sleep(cfg["refresh_interval"])
                continue

            print()
            cp(G, f"🎯 找到 {len(matched)} 個符合時段：")
            for s in matched:
                cp(G, f"   {s['timeStr']}  {s['courtName']}  (QPid={s['qpid']})")

            for slot in matched:
                ok = await book_slot(page, slot, cfg)
                if ok:
                    cp(B+G, "\n🎉 搶位成功！請確認瀏覽器中的預約結果")
                    play_sound()
                    await ainput("\n✅ 按 Enter 關閉瀏覽器…")
                    return
                # 失敗 → 回到預約頁搶下一個
                await asyncio.sleep(1)
                page = await browser.get(burl)
                await asyncio.sleep(2)

            if cfg.get("stop_on_first") and matched:
                # 所有符合時段都失敗，重新掃描
                pass

        cp(R, f"\n⛔ 已達最大重試次數 {cfg['max_retry']}，停止")

    except KeyboardInterrupt:
        print()
        cp(Y, "\n⛔ 使用者中止 (Ctrl+C)")
    except Exception as e:
        log.exception(f"未預期錯誤: {e}")
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        cp(C, "\n👋 機器人已關閉")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="竹北羽球場搶位機器人 v3")
    p.add_argument("--date",     help="預約日期 YYYY/MM/DD")
    p.add_argument("--session",  help="場次 1=早上 2=下午 3=晚上")
    p.add_argument("--times",    help="目標時段，逗號分隔，例: 09:00~10:00,10:00~11:00")
    p.add_argument("--courts",   help="偏好場地，逗號分隔，例: 2F羽 1,4F羽B2")
    p.add_argument("--interval", type=float, help="刷新間隔秒數")
    p.add_argument("--open-at",  dest="open_at", help="開放時間 YYYY/MM/DD HH:MM:SS")
    args = p.parse_args()

    if args.date:     CONFIG["target_date"]       = args.date
    if args.session:  CONFIG["session"]            = args.session
    if args.times:    CONFIG["target_times"]       = [t.strip() for t in args.times.split(",")]
    if args.courts:   CONFIG["prefer_courts"]      = [c.strip() for c in args.courts.split(",")]
    if args.interval: CONFIG["refresh_interval"]   = args.interval
    if args.open_at:  CONFIG["booking_open_time"]  = args.open_at

    uc.loop().run_until_complete(run(CONFIG))