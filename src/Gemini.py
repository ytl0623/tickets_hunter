import asyncio
import nodriver as uc
import logging
import sys

# 設定日誌格式
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

async def main():
    logging.info("啟動瀏覽器...")
    browser = await uc.start()
    
    # --- 參數設定 ---
    target_date = "2026/04/01"
    time_period = "3" # 1: 早上(06-12), 2: 下午(12-18), 3: 晚上(18-22)
    target_time_slot = "06:00~07:00" # 目標精準時段
    
    # 組合目標網址
    target_url = f"https://fe.xuanen.com.tw/fe02.aspx?module=net_booking&files=booking_place&StepFlag=2&PT=1&D={target_date}&D2={time_period}"
    
    logging.info(f"前往預約頁面: {target_url}")
    tab = await browser.get(target_url)
    
    # ==========================================
    
    # 2. 暫停並等待登入確認
    # 使用 asyncio.to_thread 確保等待輸入時，不會導致瀏覽器 CDP 連線斷開或逾時
    await asyncio.to_thread(
        input, 
        "\n🔔 網頁已載入！請在瀏覽器中確認是否已「完成登入」。\n👉 準備好後，請在此處按下 [Enter] 鍵開始自動搶票..."
    )
    # ==========================================
    
    logging.info("確認登入！開始執行自動監控與搶票...")

    # 進入主動輪詢監控
    while True:
        logging.info(f"掃描 {target_date} [{target_time_slot}] 可用場地中...")
        await asyncio.sleep(0.8) # 緩衝時間，避免 Cloudflare 阻擋
        
        # 透過 tab.evaluate 注入並執行 JavaScript
        js_code = f"""
            (() => {{
                let btns = document.querySelectorAll('img[name="PlaceBtn"]');
                for (let btn of btns) {{
                    let onclickStr = btn.getAttribute('onclick') || '';
                    if (onclickStr.includes('{target_time_slot}')) {{
                        // 攔截並自動同意 confirm 彈窗
                        window.confirm = function() {{ return true; }};
                        
                        // 點擊目標按鈕
                        btn.click();
                        return true;
                    }}
                }}
                return false; 
            }})();
        """
        
        # 執行腳本並取得結果
        is_clicked = await tab.evaluate(js_code)
        
        if is_clicked:
            logging.info(f"🎉 成功發現並點擊 {target_time_slot} 的場地！")
            break 
        else:
            logging.info(f"目前無 {target_time_slot} 可用場地，重新整理頁面...")
            await tab.reload()
            await asyncio.sleep(0.1) 

    logging.info("請接手完成後續驗證與結帳流程！")
    # 保持瀏覽器開啟10分鐘，讓您有充裕時間結帳
    await asyncio.sleep(600)

if __name__ == '__main__':
    uc.loop().run_until_complete(main())