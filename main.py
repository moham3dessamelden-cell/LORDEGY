import os
import re
import threading
from flask import Flask, request, jsonify
import telebot

# ضع توكن البوت الخاص بك هنا
API_TOKEN = '8411882850:AAG8PT5436WJlo1CKTaFiI-RMi1MWFfXgvw'
bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)

# قاعدة بيانات مؤقتة لتخزين أرقام العملاء (يفضل ربطها بـ SQLite لاحقاً)
pending_orders = {}

# إعدادات الخدمة
SERVICE_PRICE = 100  # السعر المطلوب بالجنيه
MY_VODAFONE_NUMBER = "01070417791" # رقم فودافون كاش الخاص بك

# ----------------- أوامر بوت التليجرام -----------------

@bot.message_handler(commands=['start'])
def send_welcome(message):
    msg = f"أهلاً بك! لشراء الخدمة، قم بتحويل {SERVICE_PRICE} جنيه إلى الرقم {MY_VODAFONE_NUMBER}\n\nبعد التحويل، أرسل رقم هاتفك الذي قمت بالتحويل منه في رسالة هنا للتحقق."
    bot.send_message(message.chat.id, msg)

@bot.message_handler(func=lambda message: True)
def save_phone_number(message):
    phone = message.text.strip()
    
    # تحقق من أن المدخل رقم هاتف مصري صحيح مكون من 11 رقم
    if phone.isdigit() and len(phone) == 11 and phone.startswith("01"):
        pending_orders[phone] = message.chat.id
        bot.send_message(message.chat.id, f"تم تسجيل رقمك ({phone}).\nالبوت الآن يراقب التحويلات... سيتم إرسال الخدمة لك فور وصول التحويل.")
    else:
        bot.send_message(message.chat.id, "يرجى إرسال رقم هاتف صحيح (مثال: 01012345678).")

# ----------------- خادم استقبال الـ SMS -----------------

@app.route('/sms-webhook', methods=['POST'])
def receive_sms():
    data = request.json
    if not data or 'sms_text' not in data:
        return jsonify({"error": "Bad Request"}), 400

    sms_text = data['sms_text']

    # استخراج المبلغ ورقم المرسل من رسالة فودافون كاش
    # مثال لرسالة فودافون: "تم استلام مبلغ 100 ج.م من الرقم 01012345678"
    amount_match = re.search(r'مبلغ\s*(\d+)', sms_text)
    phone_match = re.search(r'الرقم\s*(01[0125][0-9]{8})', sms_text)

    if amount_match and phone_match:
        amount = int(amount_match.group(1))
        sender_phone = phone_match.group(1)

        # التحقق مما إذا كان الرقم مسجل في الطلبات والمبلغ صحيح
        if sender_phone in pending_orders and amount >= SERVICE_PRICE:
            chat_id = pending_orders[sender_phone]
            
            # إرسال المنتج أو الخدمة للعميل
            bot.send_message(chat_id, "✅ تم استلام الدفع بنجاح!\n\nإليك كود تفعيل الخدمة الخاص بك:\n`VIP-CODE-2026`", parse_mode="Markdown")
            
            # مسح الطلب بعد التسليم
            del pending_orders[sender_phone]
            return jsonify({"status": "success", "message": "Service delivered"}), 200

    return jsonify({"status": "ignored", "message": "No matching order or invalid amount"}), 200

# ----------------- تشغيل البوت والخادم معاً -----------------

if __name__ == '__main__':
    # تشغيل البوت في مسار منفصل
    bot_thread = threading.Thread(target=bot.polling, kwargs={"non_stop": True})
    bot_thread.start()
    
    # أخذ البورت من منصة ريندر أو استخدام 5000 للتجربة
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
