# 墾丁微風海景民宿 (Kenting Breeze B&B) 線上客服 AI Agent

本專案為高科大資訊管理研究所（電子商務分組）「深度學習與行動科技商務」課程第 7 週作業。建置了一個基於 **Python、Gradio、SQLite 資料庫與 OpenAI 相容 API (Gemini)** 的智慧客服 AI Agent，提供即時的民宿資訊查詢、房型價格檢索、智慧費用試算（含連住優惠）以及真人客服轉接功能。

---

## 🛠️ 專案特色與架構

1. **資料與邏輯分離 (Grounding)**：
   - 所有民宿規定與常見問題存放於 `room_info.csv`，房型與加購服務存放於 `room.csv`，並在系統啟動時自動載入至 **SQLite 資料庫**（`breeze_bnb.db`）。
   - **嚴格規範 AI 絕不胡言亂語（No Hallucination）**：模型必須透過工具呼叫（Tool Calling）讀取資料庫真實內容來回答。
2. **客製化專屬工具 (Custom Tools)**：
   - `get_bnb_info`：查詢入住時間、早餐、停車、寵物政策等基本規定。
   - `get_room_service`：透過關鍵字模糊搜尋房型、價格、票券及加購服務與即時庫存狀態。
   - `calculate_stay_cost`：由後端程式執行精準的費用加總，並支援**同房型連續入住 2 晚以上自動享有 95 折優惠**。
   - `transfer_to_human`：針對客訴或特殊專案需求，安全地引導轉接真人櫃台處理。
3. **直覺的 Gradio 互動介面**：
   - 採用左右分欄設計（`gr.Blocks()`），左側為流暢的聊天對話視窗，右側即時顯示 **「Tools used (工具軌跡)」面板**，清楚呈現每一步驟 AI 呼叫了哪個工具、傳入的參數及回傳結果。

---

## 📂 專案檔案結構

```text
├── app.py                 # 主程式：包含 Gradio 介面、資料庫初始化與 Agent 執行迴圈
├── room_info.csv          # 民宿基本規範與常見問題資料來源
├── room.csv               # 房型、加購服務與價格狀態資料來源
├── breeze_bnb.db          # SQLite 本機資料庫（執行後自動生成）
├── requirements.txt       # Python 套件相依性清單
└── README.md              # 專案說明文件
