# 平台實作參考：CTBC Sports 中信育樂售票

**文件說明**：中信育樂售票網 (`tix.ctbcsports.com`) 平台的完整實作參考，涵蓋宏碁 Utiki / UTK 架構、會員登入、優先預購（VIP 驗證）、場次與區域選擇、OCR 驗證碼、以及購物車結帳自動化等技術實作指南。
**最後更新**：2026-10-06

---

## 平台概述

**平台名稱**：中信育樂售票網 (CTBC Sports)
**關聯網站**：
- `tix.ctbcsports.com` - 主站
- `tix.ctbcsports.com/DEA/` - 新北中信特攻籃球隊
- `tix.ctbcsports.com/BROTHERS/` - 中信兄弟棒球隊

**主要業務**：台灣職業運動賽事（籃球、棒球）、季票、會員招募
**完成度**：92% ✅ (11/12 Stages)
**難度級別**：⭐⭐⭐ (高)

---

## 平台特性

### 核心特點
✅ **優勢**：
- 基於宏碁 Utiki (UTK) 核心引擎，API 與頁面代碼規則明確 (`UTK0101`, `UTK0201`, `UTK0206`)
- 結帳表單結構清晰，支援 APP 電子票券 (免手續費)、超商取票、信用卡與 ATM 虛擬帳號
- 驗證碼格式統一為 4 碼英數字，Canvas 提取與 OCR 辨識成功率高

⚠️ **挑戰**：
- URL 不帶 `.aspx` 副檔名（與 KHAM / 年代不同，路徑為 `/DEA/UTK0201_` 等）
- 場次列表由前端 `ShowPerformance()` 動態異步渲染至 `#PerformanceListTable`
- 優先預購需通過 `#buy` 彈窗身分驗證 (`#ID1`, `#ID2`, `#CHK`)
- 結帳流程包含二階段展開（`#Checkout` 展開 `#paybill`）

---

## 核心函式索引 (`src/platforms/ctbc.py`)

| 階段 | 函式名稱 | 說明 |
|------|---------|------|
| Main | `nodriver_ctbc_main()` | 主控制流程（URL 路由與狀態分發）|
| Stage 2 | `nodriver_ctbc_login()` | 會員登入（含 OCR 驗證碼辨識）|
| Stage 2 | `nodriver_ctbc_vip_login()` | 優先購買/VIP 驗證彈窗處理 |
| Stage 3 | `nodriver_ctbc_dismiss_dialog()` | 自動關閉阻礙性彈窗 |
| Stage 4 | `nodriver_ctbc_date_auto_select()` | 動態場次列表解析與日期關鍵字選擇 |
| Stage 5 | `nodriver_ctbc_area_auto_select()` | 票區自動選擇與條件式遞補 |
| Stage 6 | `nodriver_ctbc_assign_ticket_number()` | 自動設定購票張數（支援多票種與限購） |
| Stage 7 | `nodriver_ctbc_captcha_handler()` | 圖形驗證碼單次輪詢辨識與自動重試 |
| Stage 8 | `nodriver_ctbc_submit_cart_and_monitor()` | 快速送出購物車與低延遲伺服器回應輪詢 |
| Stage 9 & 10 | `nodriver_ctbc_checkout()` | 購物車展開、取票/付款方式選擇、條款勾選與送出結帳 |

---

## URL 路由表

| URL 模式 | 頁面型別 | 處理邏輯 |
|---------|---------|---------|
| `tix.ctbcsports.com/` / `UTK0101_` | 首頁 | 檢查會員登入狀態，自動登入 |
| `UTK0201_?PRODUCT_ID=` | 活動/場次頁 | 自動執行 `ShowPerformance()`，比對 `date_keyword` 點選購票；處理優先預購 `#buy` |
| `UTK0201_001` / `UTK0202` / `UTK0204` / `UTK0205` | 選區/選位頁 | 區域選擇、票數填寫、OCR 驗證碼輸入、加入購物車 |
| `UTK0206_` | 購物車與結帳頁 | 點擊「前往結帳」，選擇 APP 取票/信用卡付款，勾選約定條款，點擊「送出結帳」 |
