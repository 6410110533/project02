import sys
sys.path.insert(0, 'chromedriver.exe')
import re
import os
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
import requests
import json
from neo4j import GraphDatabase
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np

# ตั้งค่า Line Bot
line_bot_api = LineBotApi('NzznU7qxKlwDFA1Z9eAFrlCeff3TCYIC1/UZh0cBuJWhOO46IqJMxi0VN0NhvJ6lH7dByaBzn3X4QXvWDg/BQWfc6ONuuPxrXgjI6IF+C2himgQpcEpRJQluBJpmeJ5fCIFd9vwio3BGv9dW5fnZ4wdB04t89/1O/w1cDnyilFU=')
handler = WebhookHandler('5f6e893e3135f9f8d37f0ea4c7e67d0a')

# เชื่อมต่อ Neo4j
URI = "neo4j://localhost:7687"
AUTH = ("neo4j", "6410110533")

# ฟังก์ชันรันคำสั่งใน Neo4j
def run_query(query, parameters=None):
    driver = GraphDatabase.driver(URI, auth=AUTH)
    try:
        with driver.session() as session:
            result = session.run(query, parameters)
            return [record for record in result]
    finally:
        driver.close()

# ฟังก์ชันบันทึกประวัติการสนทนาใน Neo4j
def save_chat_history_to_neo4j(user_id, user_message, bot_message):
    cypher_query = '''
    MERGE (u:User {user_id: $user_id})
    CREATE (c:Chat {message: $user_message, timestamp: datetime()})
    CREATE (b:Bot {message: $bot_message, timestamp: datetime()})
    MERGE (u)-[:SENT]->(c)
    MERGE (c)-[:REPLIED_WITH]->(b)
    '''
    run_query(cypher_query, parameters={"user_id": user_id, "user_message": user_message, "bot_message": bot_message})

# ฟังก์ชันดึงข้อมูลจากเว็บไซต์
def scrape_website():
    url = "https://www.ccdoubleo.com/th/accessories/women/bags.html"
    response = requests.get(url)
    mysoup = BeautifulSoup(response.text, "html.parser")
    job_elements = mysoup.find_all("div", {"class": "product-item-info"})

    result = []
    for job_element in job_elements:
        title_element = job_element.find("a", class_="product-item-link")
        title_price = job_element.find("span", class_="price")
        link_element = job_element.find("a", class_="product-item-link")["href"]

        # เพิ่มส่วนการดึงข้อมูล item_id
        script_tag = job_element.find("a", onclick=True)
        item_id = re.search(r'"item_id":"(.*?)"', script_tag["onclick"]).group(1)

        price_value = title_price.text.strip()
        result.append({
            'title': title_element.text.strip(),
            'price': price_value,
            'item_id': item_id,
            'link': link_element
        })
    
    return result

# สร้างโมเดล SentenceTransformer สำหรับค้นหาความใกล้เคียง
encoder = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

# การเตรียม faiss index เพื่อค้นหาความใกล้เคียง
def create_faiss_index(phrases):
    vectors = encoder.encode(phrases)
    vector_dimension = vectors.shape[1]
    index = faiss.IndexFlatL2(vector_dimension)
    faiss.normalize_L2(vectors)
    index.add(vectors)
    return index, vectors

# สร้างประโยคตัวอย่างสำหรับการค้นหา intent
intent_phrases = [
    "กระเป๋าผ้า",
    "กระเป๋าคาดเอว",
    "กระเป๋าสะพายไหล่",
    "กระเป๋าไนลอน",
    "กระเป๋าหนัง"
]
index, vectors = create_faiss_index(intent_phrases)

# ฟังก์ชันสำหรับค้นหาข้อความที่ใกล้เคียงที่สุดด้วย FAISS
def faiss_search(user_context):
    search_vector = encoder.encode(user_context['category'])
    _vector = np.array([search_vector])
    faiss.normalize_L2(_vector)
    distances, ann = index.search(_vector, k=1)

    distance_threshold = 0.5
    if distances[0][0] > distance_threshold:
        return 'unknown'
    else:
        return intent_phrases[ann[0][0]]

# ฟังก์ชันสร้างคำตอบแบบมีบุคลิกและกรองตามช่วงราคา
def generate_personalized_response(intent, scraped_data, user_context):
    intent = faiss_search(user_context)
    response = ""

    # ตรวจสอบช่วงราคาที่ผู้ใช้เลือก
    price_range = user_context['price_range']
    
    # ฟังก์ชันช่วยในการกรองราคาตามช่วงที่กำหนด
    def filter_by_price_range(products, min_price=None, max_price=None):
        filtered = []
        for product in products:
            price = float(re.sub(r'[^\d.]', '', product['price']))  # แปลงราคาเป็น float
            if (min_price is None or price >= min_price) and (max_price is None or price <= max_price):
                filtered.append(product)
        return filtered
    
    # กำหนดช่วงราคาจากคำสั่งของผู้ใช้
    if price_range == "ต่ำกว่า 500 บาท":
        min_price, max_price = None, 500
    elif price_range == "500-1000 บาท":
        min_price, max_price = 500, 1000
    elif price_range == "มากกว่า 1000 บาท":
        min_price, max_price = 1000, None
    else:
        min_price, max_price = None, None  # ถ้าไม่ได้ระบุช่วงราคา ใช้ทั้งหมด

    # เงื่อนไขการค้นหาตามประเภทสินค้าที่ผู้ใช้สนใจ
    if "กระเป๋าผ้า" in intent:
        filtered_products = filter_by_price_range([product for product in scraped_data if "ผ้า" in product['title']], min_price, max_price)
        if not filtered_products:
            response = "ไม่พบกระเป๋าผ้าสำหรับคุณในช่วงราคานี้ค่ะ"
        else:
            response = "เราเจอสินค้าที่ตรงกับที่คุณต้องการค่ะ (เรียงตามราคา):\n"
            for product in filtered_products[:3]:
                response += f"- {product['title']} ราคา: {product['price']} บาท\nลิงก์: {product['link']}\n"

    elif "กระเป๋าคาดเอว" in intent:
        filtered_products = filter_by_price_range([product for product in scraped_data if "คาดเอว" in product['title']], min_price, max_price)
        if not filtered_products:
            response = "ไม่พบกระเป๋าคาดเอวสำหรับคุณในช่วงราคานี้ค่ะ"
        else:
            response = "เราเจอสินค้าที่ตรงกับที่คุณต้องการค่ะ (เรียงตามราคา):\n"
            for product in filtered_products[:3]:
                response += f"- {product['title']} ราคา: {product['price']} บาท\nลิงก์: {product['link']}\n"

    elif "กระเป๋าสะพายไหล่" in intent:
        filtered_products = filter_by_price_range([product for product in scraped_data if "สะพายไหล่" in product['title']], min_price, max_price)
        if not filtered_products:
            response = "ไม่พบกระเป๋าสะพายไหล่สำหรับคุณในช่วงราคานี้ค่ะ"
        else:
            response = "เราเจอสินค้าที่ตรงกับที่คุณต้องการค่ะ (เรียงตามราคา):\n"
            for product in filtered_products[:3]:
                response += f"- {product['title']} ราคา: {product['price']} บาท\nลิงก์: {product['link']}\n"

    elif "กระเป๋าไนลอน" in intent:
        filtered_products = filter_by_price_range([product for product in scraped_data if "ไนลอน" in product['title']], min_price, max_price)
        if not filtered_products:
            response = "ไม่พบกระเป๋าไนลอนสำหรับคุณในช่วงราคานี้ค่ะ"
        else:
            response = "เราเจอสินค้าที่ตรงกับที่คุณต้องการค่ะ (เรียงตามราคา):\n"
            for product in filtered_products[:3]:
                response += f"- {product['title']} ราคา: {product['price']} บาท\nลิงก์: {product['link']}\n"

    elif "กระเป๋าหนัง" in intent:
        filtered_products = filter_by_price_range([product for product in scraped_data if "หนัง" in product['title']], min_price, max_price)
        if not filtered_products:
            response = "ไม่พบกระเป๋าหนังสำหรับคุณในช่วงราคานี้ค่ะ"
        else:
            response = "เราเจอสินค้าที่ตรงกับที่คุณต้องการค่ะ (เรียงตามราคา):\n"
            for product in filtered_products[:3]:
                response += f"- {product['title']} ราคา: {product['price']} บาท\nลิงก์: {product['link']}\n"

    else:
        response = "ขอโทษค่ะ ไม่เข้าใจคำค้นของคุณ"

    return response

# ฟังก์ชันสำหรับส่ง Quick Reply เพื่อเลือกประเภทสินค้า หรือช่วงราคา
def ask_initial_questions(reply_token, question_type):
    if question_type == 'category':
        # Quick Reply สำหรับเลือกประเภทสินค้า
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="กระเป๋า", text="กระเป๋า")),
            QuickReplyButton(action=MessageAction(label="เครื่องประดับ", text="เครื่องประดับ")),
        ])
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="กรุณาเลือกประเภทสินค้าที่คุณสนใจ:", quick_reply=quick_reply)
        )
    elif question_type == 'price_range':
        # Quick Reply สำหรับเลือกช่วงราคา
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="ต่ำกว่า 500 บาท", text="ต่ำกว่า 500 บาท")),
            QuickReplyButton(action=MessageAction(label="500-1000 บาท", text="500-1000 บาท")),
            QuickReplyButton(action=MessageAction(label="มากกว่า 1000 บาท", text="มากกว่า 1000 บาท")),
        ])
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="กรุณาเลือกช่วงราคาที่คุณสนใจ:", quick_reply=quick_reply)
        )

# ฟังก์ชันสำหรับ Flask API เพื่อใช้กับ Line Bot
app = Flask(__name__)

# สร้างตัวแปรเก็บ context และประวัติการสนทนาของผู้ใช้แต่ละคน
user_contexts = {}

@app.route("/", methods=['POST'])
def linebot():
    body = request.get_data(as_text=True)
    try:
        json_data = json.loads(body)
        msg = json_data['events'][0]['message']['text'].lower()  # เปลี่ยนข้อความเป็นตัวพิมพ์เล็กเพื่อให้ง่ายต่อการเปรียบเทียบ
        tk = json_data['events'][0]['replyToken']
        user_id = json_data['events'][0]['source']['userId']

        # ตรวจสอบคำว่า "สวัสดี" และ "ขอบคุณ"
        if "สวัสดี" in msg:
            line_bot_api.reply_message(tk, TextSendMessage(text="สวัสดีค่ะ! ฉันคือบอทที่ช่วยให้คุณค้นหาสินค้ากระเป๋าตามประเภทที่คุณสนใจ จะมีประเภท กระเป๋าหนัง , กระเป๋าผ้า , กระเป๋าคาดเอว ,กระเป๋าไนลอน และ กระเป๋าสะพายไหล่ ค่ะ"))
            return 'OK'
        elif "ขอบคุณ" in msg:
            line_bot_api.reply_message(tk, TextSendMessage(text="ยินดีค่ะ! <3"))
            return 'OK'

        # ตรวจสอบ context ของผู้ใช้
        if user_id not in user_contexts:
            user_contexts[user_id] = {'category': None, 'price_range': None}

        user_context = user_contexts[user_id]

        # ถ้าผู้ใช้ยังไม่ได้เลือกประเภทสินค้า ให้ถามเลือกประเภทสินค้า
        if user_context['category'] is None:
            user_contexts[user_id]['category'] = msg  # เก็บประเภทสินค้าที่ผู้ใช้เลือก
            ask_initial_questions(tk, 'price_range')  # ถามช่วงราคา
        elif user_context['price_range'] is None:
            user_contexts[user_id]['price_range'] = msg  # เก็บช่วงราคาที่ผู้ใช้เลือก

            # ค้นหา intent ที่ใกล้เคียงโดยใช้ user_context['category']
            intent = faiss_search(user_context)

            # ดึงข้อมูลจากเว็บไซต์
            scraped_data = scrape_website()

            # สร้างคำตอบพร้อมบุคลิกและช่วงราคา
            response_msg = generate_personalized_response(intent, scraped_data, user_context)

            # บันทึกการสนทนาใน Neo4j
            save_chat_history_to_neo4j(user_id, msg, response_msg)

            # ส่งข้อความตอบกลับ
            line_bot_api.reply_message(tk, TextSendMessage(text=response_msg))

            # รีเซ็ตสถานะของผู้ใช้หลังจากตอบคำถามเสร็จ
            user_contexts[user_id] = {'category': None, 'price_range': None}

        print(msg, tk)
    except Exception as e:
        print(f"Error: {e}")
        print(body)

    return 'OK'

if __name__ == '__main__':
    app.run(port=5000)
