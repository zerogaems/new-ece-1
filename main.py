import io
import os
import re
import sqlite3
from threading import Thread
from flask import Flask
import telebot
from telebot import types

# ==================== خادم Flask لإرضاء Render و UptimeRobot ====================
app = Flask('')


@app.route('/')
def home():
  return 'Freshman Bot is 100% Secure & Live on Render!'


def run_flask():
  port = int(os.environ.get('PORT', 8080))
  app.run(host='0.0.0.0', port=port)


# ==================== الإعدادات الأساسية ====================
BOT_TOKEN = os.environ.get(
    'BOT_TOKEN', 'ضع_التوكن_هنا_إن_لم_تستخدم_متغيرات_البيئة'
)
ADMIN_CHANNEL_ID = int(
    os.environ.get('ADMIN_CHANNEL_ID', '-5340670153')
)  # ID قناة الأدمن للتحقق
ADMIN_ID = int(
    os.environ.get('ADMIN_ID', '7547218555')
)  # Telegram ID الخاص بك كأدمن

FRESHMAN_LECTURES_ID = int(
    os.environ.get('FRESHMAN_LECTURES_ID', '-1004413316628')
)
FRESHMAN_DISCUSSION_ID = int(
    os.environ.get('FRESHMAN_DISCUSSION_ID', '-1003953300954')
)

bot = telebot.TeleBot(BOT_TOKEN)
user_sessions = {}


# ==================== تنظيف أرقام الهواتف ====================
def clean_phone(phone_str):
  if not phone_str:
    return ''
  digits = re.sub(r'\D', '', str(phone_str))
  if digits.startswith('963'):
    digits = '0' + digits[3:]
  elif digits.startswith('00963'):
    digits = '0' + digits[5:]
  if len(digits) == 9 and digits.startswith('9'):
    digits = '0' + digits
  return digits


# ==================== تهيئة قاعدة البيانات ====================
def init_db():
  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS freshmen (
            telegram_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  conn.commit()
  conn.close()


init_db()


# ==================== بدء التسجيل للمستجدين (/start) ====================
@bot.message_handler(commands=['start'])
def start_freshman(message):
  user_id = message.from_user.id

  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute(
      'SELECT status, full_name FROM freshmen WHERE telegram_id = ?', (user_id,)
  )
  user = cursor.fetchone()
  conn.close()

  if user:
    status, full_name = user
    if status == 'approved':
      bot.send_message(
          message.chat.id,
          f'🎉 **أهلاً بك مجدداً يا {full_name}!**\n\nلقد تم توثيق حسابك مسبقاً'
          ' واستلام روابط السنة الأولى.',
          parse_mode='Markdown',
      )
      return
    elif status == 'pending':
      bot.send_message(
          message.chat.id,
          '⏳ **طلبك قيد المراجعة حالياً من قبل الهيئة.**\n\nيرجى الانتظار،'
          ' وسيصلك رابط الانضمام هنا فور التدقيق والقبول.',
          parse_mode='Markdown',
      )
      return

  user_sessions[user_id] = {'step': 'NAME'}

  welcome_text = (
      '🎓 **أهلاً بك في بوت توثيق الطلاب المستجدين (قسم الهندسة الإلكترونية'
      ' والاتصالات - الهمك)**\n\n'
      'للانضمام لقناة ومجموعة السنة الأولى، يرجى إكمال خطوات التوثيق للتأكد'
      ' من قبولك بالقسم.\n\n'
      '✍️ **الخطوة (1/4): يرجى كتابة اسمك الثلاثي الكامل:**'
  )
  bot.send_message(
      message.chat.id,
      welcome_text,
      reply_markup=types.ReplyKeyboardRemove(),
      parse_mode='Markdown',
  )


# ==================== استقبال الاسم ورقم الهاتف ====================
@bot.message_handler(
    func=lambda msg: msg.from_user.id in user_sessions
    and user_sessions[msg.from_user.id]['step'] == 'NAME'
)
def handle_name(message):
  user_id = message.from_user.id
  full_name = message.text.strip()

  user_sessions[user_id]['name'] = full_name
  user_sessions[user_id]['step'] = 'PHONE'

  markup = types.ReplyKeyboardMarkup(
      row_width=1, resize_keyboard=True, one_time_keyboard=True
  )
  button = types.KeyboardButton(
      text='📱 مشاركة رقم الهاتف لتأكيد الهوية', request_contact=True
  )
  markup.add(button)

  bot.send_message(
      message.chat.id,
      f'أهلاً بك يا **{full_name}**! 👋\n\n'
      '📱 **الخطوة (2/4): يرجى الضغط على الزر أدناه لمشاركة رقم هاتفك'
      ' المعتمد:**',
      reply_markup=markup,
      parse_mode='Markdown',
  )


@bot.message_handler(
    content_types=['contact'],
    func=lambda msg: msg.from_user.id in user_sessions
    and user_sessions[msg.from_user.id]['step'] == 'PHONE',
)
def handle_phone(message):
  if not message.contact:
    return

  user_id = message.from_user.id

  # حماية ضد الأرقام الوهمية (تأكد أن الكرت المرسل يخص صاحب الحساب نفسه)
  if message.contact.user_id != user_id:
    bot.send_message(
        message.chat.id,
        '⚠️ **تنبيه:** يرجى مشاركة رقم الهاتف الخاص بحسابك الحالي حصراً بالضغط'
        ' على الزر.',
    )
    return

  raw_phone = message.contact.phone_number
  phone = clean_phone(raw_phone)

  user_sessions[user_id]['phone'] = phone
  user_sessions[user_id]['step'] = 'ADMISSION_PHOTO'

  bot.send_message(
      message.chat.id,
      '✅ تم التحقق من رقم الهاتف بنجاح!\n\n'
      '📄 **الخطوة (3/4): يرجى إرسال صورة بطاقة المفاضلة** (التي تظهر اسمك وقبولك'
      ' في قسم الاتصالات):',
      reply_markup=types.ReplyKeyboardRemove(),
      parse_mode='Markdown',
  )


# ==================== استقبال الصور والإنهاء ====================
@bot.message_handler(
    content_types=['photo'],
    func=lambda msg: msg.from_user.id in user_sessions
    and user_sessions[msg.from_user.id]['step'] == 'ADMISSION_PHOTO',
)
def handle_admission_photo(message):
  user_id = message.from_user.id
  photo_id = message.photo[-1].file_id

  user_sessions[user_id]['admission_photo'] = photo_id
  user_sessions[user_id]['step'] = 'ID_PHOTO'

  bot.send_message(
      message.chat.id,
      '✅ تم استلام صورة المفاضلة بنجاح!\n\n'
      '🪪 **الخطوة (4/4) والأخيرة: يرجى إرسال صورة البطاقة الشخصية (الهوية):**',
  )


@bot.message_handler(
    content_types=['photo'],
    func=lambda msg: msg.from_user.id in user_sessions
    and user_sessions[msg.from_user.id]['step'] == 'ID_PHOTO',
)
def handle_id_photo(message):
  user_id = message.from_user.id
  photo_id = message.photo[-1].file_id

  user_data = user_sessions[user_id]
  full_name = user_data['name']
  phone = user_data['phone']
  admission_photo_id = user_data['admission_photo']

  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO freshmen (telegram_id, full_name, phone, status)
        VALUES (?, ?, ?, 'pending')
        ON CONFLICT(telegram_id) DO UPDATE SET full_name = excluded.full_name, phone = excluded.phone, status = 'pending'
    """,
      (user_id, full_name, phone),
  )
  conn.commit()
  conn.close()

  media = [
      types.InputMediaPhoto(
          admission_photo_id,
          caption=(
              f'📥 **طلب توثيق مستجد جديد:**\n\n'
              f'👤 **الاسم الثلاثي:** {full_name}\n'
              f'📱 **رقم الهاتف:** `{phone}`\n'
              f'🆔 **Telegram ID:** `{user_id}`\n'
              f'👤 **المعرف:** @{message.from_user.username if message.from_user.username else "لا يوجد"}'
          ),
          parse_mode='Markdown',
      ),
      types.InputMediaPhoto(photo_id),
  ]

  bot.send_media_group(ADMIN_CHANNEL_ID, media)

  markup = types.InlineKeyboardMarkup(row_width=2)
  btn_approve = types.InlineKeyboardButton(
      text='✅ قبول وتوليد الرابط', callback_data=f'approve_{user_id}'
  )
  btn_reject = types.InlineKeyboardButton(
      text='❌ رفض الطلب', callback_data=f'reject_menu_{user_id}'
  )
  markup.add(btn_approve, btn_reject)

  bot.send_message(
      ADMIN_CHANNEL_ID,
      f'📌 **قرار الطلب الخاص بالطالب:** {full_name} (`{phone}`)',
      reply_markup=markup,
      parse_mode='Markdown',
  )

  bot.send_message(
      message.chat.id,
      '🚀 **تم استلام بياناتك ورقم هاتفك بنجاح!**\n\n'
      'طلبك الآن قيد المراجعة والتدقيق من قبل أعضاء الهيئة، وسيصلك إشعار بالقبول'
      ' مع روابط القنوات هنا فور إتمام المراجعة.',
      parse_mode='Markdown',
  )

  del user_sessions[user_id]


# ==================== التعامل مع قرارات الأدمن والأزرار ====================
@bot.callback_query_handler(
    func=lambda call: call.data.startswith(
        ('approve_', 'reject_', 'retry_freshman')
    )
)
def handle_admin_decision(call):
  data = call.data

  # إعادة المحاولة من قبل الطالب فوراً
  if data == 'retry_freshman':
    user_id = call.from_user.id
    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE freshmen SET status = 'reset' WHERE telegram_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()

    bot.answer_callback_query(call.id, '👍 تم فتح التسجيل مجدداً.')
    start_freshman(call.message)
    return

  # القبول
  if data.startswith('approve_'):
    target_user_id = int(data.split('_')[1])

    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT full_name, phone FROM freshmen WHERE telegram_id = ?',
        (target_user_id,),
    )
    st = cursor.fetchone()

    if not st:
      bot.answer_callback_query(call.id, '❌ لم يتم العثور على الطلب.')
      conn.close()
      return

    full_name, phone = st

    try:
      lectures_link = bot.create_chat_invite_link(
          chat_id=FRESHMAN_LECTURES_ID,
          member_limit=1,
          expire_date=call.message.date + 600,
      ).invite_link
      discussion_link = bot.create_chat_invite_link(
          chat_id=FRESHMAN_DISCUSSION_ID,
          member_limit=1,
          expire_date=call.message.date + 600,
      ).invite_link

      cursor.execute(
          "UPDATE freshmen SET status = 'approved' WHERE telegram_id = ?",
          (target_user_id,),
      )
      conn.commit()

      markup = types.InlineKeyboardMarkup(row_width=1)
      btn1 = types.InlineKeyboardButton(
          text='📚 الانضمام لقناة المحاضرات (سنة أولى)', url=lectures_link
      )
      btn2 = types.InlineKeyboardButton(
          text='💬 الانضمام لمجموعة المناقشة (سنة أولى)', url=discussion_link
      )
      markup.add(btn1, btn2)

      success_msg = (
          f'🎉 **مبارك قبولك وتوثيق حسابك يا {full_name}!**\n\n'
          f'أهلاً بك رسمياً في قسم الهندسة الإلكترونية والاتصالات 🎓\n\n'
          f'👇 **إليك روابط الانضمام الخاصة بدفعتك (سنة أولى مستجدين):**'
      )
      bot.send_message(
          target_user_id, success_msg, reply_markup=markup, parse_mode='Markdown'
      )

      admin_name = call.from_user.first_name
      bot.edit_message_text(
          f'✅ **تم قبول الطالب ({full_name} - {phone}) بنجاح بواسطة المشرف'
          f' {admin_name}.**',
          chat_id=call.message.chat.id,
          message_id=call.message.message_id,
      )

    except Exception as e:
      bot.send_message(
          call.message.chat.id, f'❌ حدث خطأ أثناء توليد الروابط: {str(e)}'
      )

    conn.close()

  # قائمة أسباب الرفض
  elif data.startswith('reject_menu_'):
    target_user_id = int(data.split('_')[2])
    markup = types.InlineKeyboardMarkup(row_width=1)

    r1 = types.InlineKeyboardButton(
        text='📷 الصورة غير واضحة',
        callback_data=f'reject_reason_{target_user_id}_1',
    )
    r2 = types.InlineKeyboardButton(
        text='📄 المفاضلة لا تحتوي قسم اتصالات',
        callback_data=f'reject_reason_{target_user_id}_2',
    )
    r3 = types.InlineKeyboardButton(
        text='🪪 صورة الهوية غير مطابقة',
        callback_data=f'reject_reason_{target_user_id}_3',
    )

    markup.add(r1, r2, r3)
    bot.edit_message_text(
        '📌 **اختر سبب رفض الطلب ليتم إبلاغ الطالب:**',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
    )

  # إرسال سبب الرفض وإضافة زر الإعادة الفورية
  elif data.startswith('reject_reason_'):
    parts = data.split('_')
    target_user_id = int(parts[2])
    reason_code = parts[3]

    reasons = {
        '1': '📷 الصور المرفقة غير واضحة، يرجى إعادة التصوير بشكل جلي والإرسال مجدداً.',
        '2': '📄 بطاقة المفاضلة المرفقة لا توضح القبول في قسم الهندسة الإلكترونية والاتصالات.',
        '3': '🪪 صورة الهوية الشخصية غير واضحة أو غير مطابقة للبيانات.',
    }
    selected_reason = reasons.get(reason_code, 'الصور غير مطابقة للشروط.')

    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE freshmen SET status = 'rejected' WHERE telegram_id = ?",
        (target_user_id,),
    )
    conn.commit()
    conn.close()

    markup = types.InlineKeyboardMarkup()
    btn_retry = types.InlineKeyboardButton(
        text='🔄 إعادة رفع الصور والبيانات', callback_data='retry_freshman'
    )
    markup.add(btn_retry)

    bot.send_message(
        target_user_id,
        f'❌ **عذراً، تعذر قبول طلب التوثيق الخاص بك.**\n\n'
        f'📌 **السبب:** {selected_reason}\n\n'
        f'👇 **يمكنك إفادة البيانات وإعادة المحاولة بضغطة زر:**',
        reply_markup=markup,
        parse_mode='Markdown',
    )

    admin_name = call.from_user.first_name
    bot.edit_message_text(
        f'❌ **تم رفض الطلب بواسطة المشرف {admin_name}.**\nالسبب: {selected_reason}',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )


# ==================== أوامر الإحصائيات والبحث للأدمن ====================
@bot.message_handler(commands=['stats_freshmen'])
def stats_freshmen(message):
  if message.from_user.id != ADMIN_ID:
    return
  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute(
      "SELECT status, COUNT(*) FROM freshmen GROUP BY status"
  )
  stats = cursor.fetchall()
  conn.close()

  text = '📊 **إحصائيات توثيق المستجدين:**\n\n'
  for status, count in stats:
    text += f'• {status}: **{count}** طالب\n'
  bot.reply_to(message, text, parse_mode='Markdown')


# ==================== التشغيل ====================
def run_bot():
  bot.infinity_polling(skip_pending=True)


if __name__ == '__main__':
  bot_thread = Thread(target=run_bot)
  bot_thread.daemon = True
  bot_thread.start()

  run_flask()

