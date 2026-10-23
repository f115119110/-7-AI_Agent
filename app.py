"""
A customer service agent for Kenting Breeze B&B (Gradio app).

Every question goes to the agent first. The agent decides which tool to use:
B&B information and room/service rates come from a SQLite database,
the code calculates stay costs and add-ons (including consecutive-night discounts),
and anything the tools cannot answer is handed to a human.
The panel on the right shows which tools were called and what they returned.

Run it with:  python app-gradio.py
"""

import csv
import json
import os
import re
from pathlib import Path
import sqlite3

import gradio as gr
from openai import OpenAI
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 1. API setup
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env" if "__file__" in locals() else ".env")
CHAT_MODEL = os.getenv("MODEL_NAME", "gemini-3.5-flash-lite")
client = OpenAI(
    api_key=os.getenv("API_KEY"),
    base_url=os.getenv("LLM_API_URL"),
    timeout=60.0
)

# ---------------------------------------------------------------------------
# 2. The B&B database: two tables, room_info and room_services (SQLite)
# ---------------------------------------------------------------------------

connection = sqlite3.connect("breeze_bnb_app.db", check_same_thread=False)
connection.row_factory = sqlite3.Row
cursor = connection.cursor()

cursor.execute("DROP TABLE IF EXISTS room_info")
cursor.execute("CREATE TABLE room_info (topic TEXT PRIMARY KEY, content TEXT NOT NULL)")

cursor.execute("DROP TABLE IF EXISTS room_services")
cursor.execute("""CREATE TABLE room_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    available INTEGER NOT NULL
)""")

# 讀取 room_info.csv
room_info_file = Path(__file__).parent / "room_info.csv" if "__file__" in locals() else Path("room_info.csv")
with open(room_info_file, newline="", encoding="utf-8") as f:
    cursor.executemany(
        "INSERT INTO room_info (topic, content) VALUES (?, ?)",
        [(r["topic"], r["content"]) for r in csv.DictReader(f)]
    )

# 讀取 rooms.csv（若命名為 room.csv 亦自動相容）
rooms_file = Path(__file__).parent / "rooms.csv" if "__file__" in locals() else Path("rooms.csv")
if not rooms_file.exists():
    rooms_file = Path(__file__).parent / "room.csv" if "__file__" in locals() else Path("room.csv")

with open(rooms_file, newline="", encoding="utf-8") as f:
    records = []
    for r in csv.DictReader(f):
        is_avail = 1 if r["available"].strip().lower() == "true" else 0
        records.append((r["name"], int(r["price"]), is_avail))

    cursor.executemany(
        "INSERT INTO room_services (name, price, available) VALUES (?, ?, ?)",
        records
    )
connection.commit()

BNB_TOPICS = [r["topic"] for r in cursor.execute("SELECT topic FROM room_info")]
print(f"Ready: {len(BNB_TOPICS)} B&B topics loaded.")

# ---------------------------------------------------------------------------
# 3. The tools, as plain functions, and what the model sees
# ---------------------------------------------------------------------------

def get_bnb_info(topic):
    """Return one piece of information about Kenting Breeze B&B (check-in, parking, breakfast, etc.)."""
    row = cursor.execute("SELECT content FROM room_info WHERE topic = ?", (topic.strip().lower(),)).fetchone()
    if row is None:
        return {"found": False, "topics_available": BNB_TOPICS}
    return {"found": True, "topic": topic, "content": row["content"]}


def get_room_service(item_name):
    """Find room types, tickets, transport, or rental services and return prices and availability."""
    like = f"%{item_name.strip()}%"
    rows = cursor.execute(
        "SELECT name, price, available FROM room_services WHERE name LIKE ?",
        (like,)
    ).fetchall()
    if not rows:
        return {"found": False, "note": f"查無包含 '{item_name}' 的房型或服務項目。"}
    return {
        "found": True,
        "items": [{
            "name": r["name"],
            "price": r["price"],
            "available": bool(r["available"])
        } for r in rows]
    }


def calculate_stay_cost(items, consecutive_nights: int = 1):
    """Calculate total cost for room reservations and add-on services.
    Applies a 5% discount on room types if staying 2 or more consecutive nights.
    items = [{"name": "觀海雙人木屋（平日）", "quantity": 2}, ...]
    """
    lines, raw_total = [], 0
    discount = 0

    for entry in items:
        found = get_room_service(entry["name"])
        if not found["found"] or len(found["items"]) != 1:
            matching_names = [it["name"] for it in found.get("items", [])]
            return {
                "success": False,
                "note": f"無法確認品項 '{entry['name']}'（找到匹配項目：{matching_names}）。請使用更精確的名稱或先以 get_room_service 查詢。"
            }

        item = found["items"][0]
        if not item["available"]:
            return {
                "success": False,
                "note": f"項目 '{item['name']}' 目前客滿或暫停供應，無法加入計算。"
            }

        qty = entry["quantity"]
        subtotal = item["price"] * qty
        raw_total += subtotal

        # 住宿房型判斷：名稱中包含木屋、雙人房、合院、家庭
        is_room = any(keyword in item["name"] for keyword in ["木屋", "雙人房", "合院", "家庭"])
        if is_room and consecutive_nights >= 2:
            item_discount = int(subtotal * 0.05)
            discount += item_discount
            lines.append(f"{qty} x {item['name']} = NT${subtotal}（享連住 95 折，折抵 NT${item_discount}）")
        else:
            lines.append(f"{qty} x {item['name']} = NT${subtotal}")

    final_total = raw_total - discount
    return {
        "success": True,
        "details": lines,
        "consecutive_nights": consecutive_nights,
        "original_total": raw_total,
        "discount_saved": discount,
        "final_total": final_total,
        "currency": "NT$"
    }


def transfer_to_human():
    """Hand the conversation to human staff for complaints, unanswerable questions, or manager requests."""
    return {
        "message": "請致電民宿現場服務櫃台專線 08-886-1234（服務時間為每日 08:30 至 21:30），或於現場向櫃台人員反映。",
        "note": (
            "The hand-over is done. First politely apologize or explain why in one sentence, "
            "then provide this contact message. Do not say that you will directly transfer or connect the call, "
            "because you cannot. Do not call this tool again."
        )
    }


TOOL_FUNCTIONS = {
    "get_bnb_info": get_bnb_info,
    "get_room_service": get_room_service,
    "calculate_stay_cost": calculate_stay_cost,
    "transfer_to_human": transfer_to_human,
}

topics_description = ", ".join(BNB_TOPICS)
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_bnb_info",
            "description": f"Get general information about Kenting Breeze B&B. Available topics: {topics_description}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "enum": BNB_TOPICS, "description": "The topic of information to query."}
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_room_service",
            "description": "Get the price and availability of room types, extra services, rentals, or transport tickets. Use a short keyword like '雙人', '四人', '烤肉', '接送', '水上活動'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "The room type name or service keyword to search for."}
                },
                "required": ["item_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_stay_cost",
            "description": "Calculate total cost for room reservations and add-on services. Applies a 5% discount for staying 2 or more consecutive nights in the same room. Always use this for any cost calculation; never add up prices yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "List of room types or services with quantity.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "The exact name of the room or service from the database."},
                                "quantity": {"type": "integer", "description": "Number of units or nights."}
                            },
                            "required": ["name", "quantity"]
                        }
                    },
                    "consecutive_nights": {
                        "type": "integer",
                        "description": "Number of consecutive nights stayed in the room (default is 1). Set to 2 or more to get a 5% discount."
                    }
                },
                "required": ["items"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "Use this for customer complaints, manager requests, refunds, or questions no other tool can answer.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    }
]

SYSTEM_PROMPT = (
    "You are the online customer service agent of Kenting Breeze B&B (墾丁微風海景民宿) in Kenting, Pingtung.\n"
    "All prices are in New Taiwan dollars, written like NT$3,200.\n\n"
    "RULES\n"
    "1. Answer only from tool results. Never answer from memory, and never invent room rates, policies, or availability.\n"
    "2. If you cannot find the answer from tools, say so plainly. Do not guess.\n"
    "3. Only talk about Kenting Breeze B&B. Politely refuse unrelated topics, such as general weather, homework, or other hotels.\n"
    "4. Answer politely in the customer's language (prefer Traditional Chinese), in three sentences or less.\n"
    "5. Always use the exact item name returned by the tools.\n"
    "6. B&B policies (check-in, parking, breakfast, pets, cancellation, location, amenities, wifi) -> get_bnb_info.\n"
    "7. Room rates, extra services, and availability questions -> get_room_service.\n"
    "8. Use calculate_stay_cost for any totals, package quotes, or stays. Never calculate amounts yourself.\n"
    "9. Complaints, requests for a manager, refunds, or questions no tool can answer -> transfer_to_human."
)

MAX_STEPS = 6

def run_agent(chat_history):
    """Answer the last question in chat_history. Returns (answer, trace)."""
    last_msg = chat_history[-1]
    raw_content = last_msg.get("content", "") if isinstance(last_msg, dict) else last_msg[0]

    # 防呆：確保 question 是純文字字串
    if isinstance(raw_content, list):
        question = " ".join([item if isinstance(item, str) else item.get("text", str(item)) for item in raw_content])
    elif isinstance(raw_content, dict):
        question = raw_content.get("text", str(raw_content))
    else:
        question = str(raw_content)

    language = "Traditional Chinese" if re.search(r"[\u4e00-\u9fff]", question) else "English"

    # 清理傳入 API 的對話歷史
    clean_history = []
    for msg in chat_history[:-1]:
        if isinstance(msg, dict):
            c = msg.get("content", "")
            if isinstance(c, list):
                c = " ".join([i if isinstance(i, str) else i.get("text", str(i)) for i in c])
            clean_history.append({"role": msg.get("role", "user"), "content": str(c)})
        elif isinstance(msg, (list, tuple)) and len(msg) == 2:
            u, b = msg
            if u:
                clean_history.append({"role": "user", "content": str(u)})
            if b:
                clean_history.append({"role": "assistant", "content": str(b)})

    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + clean_history
        + [{"role": "system", "content": f"Reply in {language}."}]
        + [{"role": "user", "content": question}]
    )
    trace = []

    for step in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            tools=TOOL_SPECS,
            temperature=0.2,
        )
        message = response.choices[0].message

        if not message.tool_calls:
            return (message.content or "").strip(), trace

        messages.append(message)

        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments or "{}")
            if call.function.name in TOOL_FUNCTIONS:
                try:
                    result = TOOL_FUNCTIONS[call.function.name](**arguments)
                except Exception as e:
                    result = {"status": "error", "message": f"Execution error: {str(e)}"}
            else:
                result = {"status": "error", "message": f"Tool '{call.function.name}' not found."}

            trace.append({
                "step": step + 1,
                "tool": call.function.name,
                "arguments": arguments,
                "result": result
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)
            })

    return "抱歉，目前系統處理逾時，請直接撥打民宿櫃台專線 08-886-1234 由專人為您服務。", trace

# ---------------------------------------------------------------------------
# 4. The Gradio page
# ---------------------------------------------------------------------------

GREETING = (
    "Hello! Welcome to Kenting Breeze B&B (墾丁微風海景民宿). "
    "Feel free to ask about room rates, check-in policies, or calculate your stay cost!\n\n"
    "您好！我是墾丁微風海景民宿的線上客服小幫手，很高興為您服務！\n"
    "您可以向我詢問房價、入住退房時間、早餐、停車、寵物規定或請我試算入住總費用。"
)

EMPTY_TRACE = "### Tools used\n\n(nothing yet)"

SAMPLE_GROUPS_ZH = {
    "民宿基本規定（get_bnb_info）": [
        "請問幾點可以辦理入住和退房？",
        "有提供免費停車位嗎？",
        "可以帶寵物柴犬一起入住嗎？",
        "退訂政策是怎麼規定的？",
    ],
    "房型與加購服務查詢（get_room_service）": [
        "請問觀海雙人木屋平日和假日一晚多少錢？",
        "尊榮VIP六人合院假日還有空房嗎？",
        "高鐵左營站有接送服務嗎？費用多少？",
        "租借烤肉架設備怎麼收費？",
    ],
    "住宿與加購費用試算（calculate_stay_cost）": [
        "我們想訂觀海雙人木屋平日住 2 晚，再加上 2 張水上活動套票和租一台電動機車，這樣總共多少錢？",
        "我要訂溫馨四人家庭木屋平日一晚，加一張單人床墊，再加買雙人下午茶套餐，請幫我算總共多少？",
    ],
    "客訴、特殊需求與無關話題": [
        "昨天入住時冷氣一直漏水而且有異味，我要找你們老闆投訴！",
        "我想包下下個月整棟民宿辦戶外婚禮，可以跟經理談嗎？",
        "有賣珍珠奶茶或披薩嗎？",
        "可以幫我寫期末報告嗎？",
    ],
    "多輪連續對話（依序點選）": [
        "請問觀海雙人木屋平日一晚多少錢？",
        "那我們連續住 2 晚，再加兩張水上活動套票呢？",
        "好的，那請問隔天早餐供應時間是幾點到幾點？",
    ],
}

SAMPLE_GROUPS = {
    "B&B Information (get_bnb_info)": [
        "What time is check-in and check-out?",
        "Do you provide free parking?",
        "Can I bring my pet dog?",
    ],
    "Room Rates & Availability (get_room_service)": [
        "How much is the Ocean View Double Cabin on weekdays and weekends?",
        "Is the VIP 6-Person Courtyard available on weekends?",
        "Do you have shuttle service from Zuoying THSR Station?",
    ],
    "Stay Cost Calculation (calculate_stay_cost)": [
        "I'd like to book 2 nights for Ocean View Double Cabin on weekdays, plus 2 water activity tickets and 1 e-scooter. How much is it?",
        "One night of Cozy 4-Person Cabin on weekday with 1 extra single mattress and 1 afternoon tea set, please calculate the total.",
    ],
    "Complaints & Unrelated Questions": [
        "The AC leaked and had an odor during my stay. I want to speak to the manager.",
        "Do you sell pizza?",
        "What is the weather in Kaohsiung tomorrow?",
        "Can you help me write my homework?",
    ],
    "Multi-turn conversation": [
        "How much is the Ocean View Double Cabin on weekdays?",
        "What if we stay for 2 consecutive nights with 2 water activity tickets?",
        "Great, what time is breakfast served in the morning?",
    ],
}

def format_trace(trace):
    """Show the tool calls of the last answer, so the choice is visible."""
    if not trace:
        return "### Tools used\n\n(no tool was needed for this answer)"
    lines = ["### Tools used", ""]
    for entry in trace:
        lines += [
            f"**{entry['step']}. {entry['tool']}**",
            "",
            f"arguments: `{json.dumps(entry['arguments'], ensure_ascii=False)[:160]}`",
            "",
            f"result: `{json.dumps(entry['result'], ensure_ascii=False)[:260]}`",
            "",
        ]
    return "\n".join(lines)

def start_chat():
    return [{"role": "assistant", "content": GREETING}]

def add_user_message(user_text, chat_history):
    user_text = user_text.strip()
    if not user_text:
        return "", chat_history
    return "", chat_history + [{"role": "user", "content": user_text}]

def answer_question(chat_history):
    if not chat_history:
        return chat_history, EMPTY_TRACE
    last_msg = chat_history[-1]
    is_user = (isinstance(last_msg, dict) and last_msg.get("role") == "user") or (isinstance(last_msg, (list, tuple)) and last_msg[0] and not last_msg[1])
    if not is_user:
        return chat_history, EMPTY_TRACE
    try:
        answer, trace = run_agent(chat_history)
        panel = format_trace(trace)
    except Exception as error:
        answer = f"(系統訊息) API 呼叫異常: {error}"
        panel = "### Tools used\n\n(the call failed)"
    return chat_history + [{"role": "assistant", "content": answer}], panel

with gr.Blocks(title="墾丁微風海景民宿客服 Agent") as demo:
    gr.Markdown(
        "## 墾丁微風海景民宿 (Kenting Breeze B&B) 線上客服小幫手\n"
        "所有問題均先由客服 Agent 判斷處理：民宿規定與房價自 SQLite 資料庫檢索，"
        "住宿與加購費用由程式精準試算（享有連住 2 晚 95 折優惠），無法回答或客訴則引導真人櫃台處理。"
    )

    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(value=start_chat(), height=430, label="民宿客服助理")
            with gr.Row():
                user_box = gr.Textbox(
                    placeholder="請詢問入住規定、房型房價或費用試算...",
                    show_label=False,
                    scale=8,
                )
                send_button = gr.Button("發送", variant="primary", scale=1)
                clear_button = gr.Button("清除", scale=1)

            gr.Markdown("**常見範例問題（點選後按發送）**")
            with gr.Tabs():
                with gr.Tab("中文"):
                    for group, questions in SAMPLE_GROUPS_ZH.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)
                with gr.Tab("English"):
                    for group, questions in SAMPLE_GROUPS.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)

        with gr.Column(scale=2):
            trace_panel = gr.Markdown(EMPTY_TRACE)

    send_button.click(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, chatbot, [chatbot, trace_panel]
    )
    user_box.submit(add_user_message, [user_box, chatbot], [user_box, chatbot]).then(
        answer_question, chatbot, [chatbot, trace_panel]
    )

    clear_button.click(start_chat, outputs=chatbot).then(
        lambda: ("", EMPTY_TRACE), outputs=[user_box, trace_panel]
    )

# ---------------------------------------------------------------------------
# 啟動 Gradio 服務
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("正在啟動 Gradio 服務...")
    demo.launch(inbrowser=True)