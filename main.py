import io
import os
import re
import sqlite3
from threading import Thread
from flask import Flask
import pandas as pd
import telebot
from telebot import types

# ==================== خادم Flask لإرضاء Render و UptimeRobot ====================
app = Flask('')


@app.route('/')
def home():
  return 'Freshman Verification Bot - Direct Admin Panel is Active!'


def run_flask():
  port = int(os.environ.get('PORT', 8080))
  app.run(host='0.0.0.0', port=port)


# ==================== الإعدادات الأساسية ====================
BOT_TOKEN = os.environ.get(
    'BOT_TOKEN', 'ضع_التوكن_هنا_إن_لم_تستخدم_متغيرات_البيئة'
)
ADMIN_ID = int(
    os.environ.get('ADMIN_ID', '7547218555')
)  # Telegram ID الخاص بك كأدمن رئيسي

FRESHMAN_LECTURES_ID = int(
    os.environ.get('FRESHMAN_LECTURES_ID', '-1004413316628')
)
FRESHMAN_DISCUSSION_ID = int(
    os.environ.get('FRESHMAN_DISCUSSION_ID', '--1003953300954')
)

bot = telebot.TeleBot(BOT_TOKEN)
user_sessions = {}
admin_input_states = {}


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


# ==================== تهيئة قاعدة البيانات والأرشيف ====================
def init_db():
  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS freshmen (
            telegram_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            admission_photo_id TEXT,
            id_photo_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

  cursor.execute('PRAGMA table_info(freshmen)')
  columns = [info[1] for info in cursor.fetchall()]
  if 'admission_photo_id' not in columns:
    cursor.execute('ALTER TABLE freshmen ADD COLUMN admission_photo_id TEXT')
  if 'id_photo_id' not in columns:
    cursor.execute('ALTER TABLE freshmen ADD COLUMN id_photo_id TEXT')

  conn.commit()
  conn.close()


init_db()


# ==================== لوحة تحكم الأدمن (/admin) ====================
def get_admin_keyboard():
  markup = types.InlineKeyboardMarkup(row_width=2)
  btn_stats = types.InlineKeyboardButton(
      text='📊 الإحصائيات التفصيلية', callback_data='admin_stats'
  )
  btn_archive = types.InlineKeyboardButton(
      text='📁 أرشيف ومجلدات الطلاب', callback_data='admin_archive'
  )
  btn_search = types.InlineKeyboardButton(
      text='🔍 البحث عن طالب', callback_data='admin_search'
  )
  btn_export = types.InlineKeyboardButton(
      text='📥 تنزيل تقرير Excel', callback_data='admin_export'
  )
  btn_reset = types.InlineKeyboardButton(
      text='🔓 فك قفل طالب', callback_data='admin_reset'
  )

  markup.add(btn_stats, btn_archive)
  markup.add(btn_search, btn_export)
  markup.add(btn_reset)
  return markup


@bot.message_handler(commands=['admin'])
def admin_command(message):
  if message.from_user.id != ADMIN_ID:
    bot.reply_to(message, '⚠️ عذراً، هذه اللوحة مخصصة لرئيس الهيئة/الأدمن فقط.')
    return

  text = (
      '🛠️ **لوحة تحكم رئيس الهيئة المباشرة (Admin Panel)**\n\n'
      'مرحباً بك! جميع الطلبات تصلك هنا في محادثتك المباشرة.\n'
      'يمكنك استعراض الأرشيف، الإحصائيات، والبحث عن أي طالب عبر الأزرار أدناه:'
  )
  bot.send_message(
      message.chat.id,
      text,
      reply_markup=get_admin_keyboard(),
      parse_mode='Markdown',
  )


# ==================== التفاعل مع أزرار لوحة الأدمن ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def handle_admin_panel_callbacks(call):
  if call.from_user.id != ADMIN_ID:
    bot.answer_callback_query(call.id, '⚠️ غير مصرح لك.', show_alert=True)
    return

  action = call.data

  if action == 'admin_stats':
    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM freshmen')
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM freshmen WHERE status = 'approved'")
    approved = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM freshmen WHERE status = 'pending'")
    pending = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM freshmen WHERE status = 'rejected'")
    rejected = cursor.fetchone()[0]
    conn.close()

    text = (
        f'📊 **إحصائيات توثيق المستجدين التفصيلية:**\n\n'
        f'👥 **إجمالي المتقدمين:** {total}\n'
        f'✅ **الطلبات المقبولة:** {approved}\n'
        f'⏳ **الطلبات المعلقة:** {pending}\n'
        f'❌ **الطلبات المرفوضة:** {rejected}\n'
    )
    bot.send_message(
        call.message.chat.id,
        text,
        reply_markup=get_admin_keyboard(),
        parse_mode='Markdown',
    )
    bot.answer_callback_query(call.id)

  elif action == 'admin_archive':
    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT telegram_id, full_name, status, created_at FROM freshmen ORDER'
        ' BY created_at DESC LIMIT 10'
    )
    students = cursor.fetchall()
    conn.close()

    if not students:
      bot.send_message(
          call.message.chat.id, '📁 **الأرشيف فارغ حالياً، لا يوجد طلاب.**'
      )
      bot.answer_callback_query(call.id)
      return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for tid, name, status, created_at in students:
      status_icon = (
          '✅' if status == 'approved' else ('⏳' if status == 'pending' else '❌')
      )
      btn = types.InlineKeyboardButton(
          text=f'{status_icon} {name} ({created_at[:10]})',
          callback_data=f'view_file_{tid}',
      )
      markup.add(btn)

    bot.send_message(
        call.message.chat.id,
        '📁 **سجلات وأرشيف أحدث الطلاب (اضغط على اسم الطالب لفتح مجلده'
        ' وصوره):**',
        reply_markup=markup,
    )
    bot.answer_callback_query(call.id)

  elif action == 'admin_search':
    admin_input_states[call.from_user.id] = 'awaiting_search'
    bot.send_message(
        call.message.chat.id,
        '🔍 **يرجى كتابة رقم هاتف الطالب أو Telegram ID الخاص به للبحث:**',
    )
    bot.answer_callback_query(call.id)

  elif action == 'admin_export':
    bot.answer_callback_query(call.id, '⏳ جاري استخراج تقرير Excel...')
    conn = sqlite3.connect('freshmen_students.db')
    df = pd.read_sql_query('SELECT * FROM freshmen', conn)
    conn.close()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
      df.to_excel(writer, index=False, sheet_name='Freshmen_Data')
    output.seek(0)

    bot.send_document(
        call.message.chat.id,
        document=types.InputFile(
            output, filename='Freshmen_Students_Report.xlsx'
        ),
        caption='📊 **تقرير الطلاب المستجدين وحالات التوثيق**',
        parse_mode='Markdown',
    )

  elif action == 'admin_reset':
    admin_input_states[call.from_user.id] = 'awaiting_reset'
    bot.send_message(
        call.message.chat.id,
        '🔓 **أرسل رقم هاتف الطالب أو Telegram ID لفك القفل عنه:**',
    )
    bot.answer_callback_query(call.id)


# ==================== عرض مجلد الطالب كاملاً بالصور للأدمن ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('view_file_'))
def handle_view_student_file(call):
  if call.from_user.id != ADMIN_ID:
    return

  target_user_id = int(call.data.split('_')[2])
  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute(
      'SELECT full_name, phone, admission_photo_id, id_photo_id, status,'
      ' created_at FROM freshmen WHERE telegram_id = ?',
      (target_user_id,),
  )
  student = cursor.fetchone()
  conn.close()

  if not student:
    bot.answer_callback_query(call.id, '❌ سجل الطالب غير موجود.')
    return

  full_name, phone, adm_photo, id_photo, status, created_at = student

  status_str = (
      '✅ مقبول'
      if status == 'approved'
      else ('⏳ قيد المراجعة' if status == 'pending' else '❌ مرفوض')
  )

  info_text = (
      f'📁 **مجلد الطالب الرقمي:**\n\n'
      f'👤 **الاسم الثلاثي:** {full_name}\n'
      f'📱 **رقم الهاتف:** `{phone}`\n'
      f'🆔 **Telegram ID:** `{target_user_id}`\n'
      f'📌 **حالة التوثيق:** {status_str}\n'
      f'🕒 **تاريخ التقديم:** {created_at}'
  )
  bot.send_message(call.message.chat.id, info_text, parse_mode='Markdown')

  # إرسال صورة المفاضلة المخزنة في مجلده
  if adm_photo:
    try:
      bot.send_photo(
          call.message.chat.id,
          adm_photo,
          caption=f'📄 **صورة المفاضلة للطالب:** {full_name}',
      )
    except Exception:
      pass

  # إرسال صورة الهوية مع أزرار التحكم
  if id_photo:
    try:
      markup = types.InlineKeyboardMarkup(row_width=2)
      btn_approve = types.InlineKeyboardButton(
          text='✅ قبول وتوليد الرابط', callback_data=f'approve_{target_user_id}'
      )
      btn_reject = types.InlineKeyboardButton(
          text='❌ رفض الطلب', callback_data=f'reject_menu_{target_user_id}'
      )
      markup.add(btn_approve, btn_reject)

      bot.send_photo(
          call.message.chat.id,
          id_photo,
          caption=f'🪪 **صورة الهوية للطالب:** {full_name}',
          reply_markup=markup,
      )
    except Exception:
      pass

  bot.answer_callback_query(call.id)


# ==================== استقبال مدخلات البحث وفك القفل من الأدمن ====================
@bot.message_handler(
    func=lambda msg: msg.from_user.id == ADMIN_ID
    and msg.from_user.id in admin_input_states
)
def handle_admin_search_and_reset(message):
  state = admin_input_states.get(message.from_user.id)
  query = message.text.strip()

  if state == 'awaiting_search':
    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT telegram_id, full_name, phone, status, created_at FROM freshmen'
        ' WHERE phone LIKE ? OR telegram_id LIKE ? OR full_name LIKE ?',
        (f'%{query}%', f'%{query}%', f'%{query}%'),
    )
    results = cursor.fetchall()
    conn.close()

    if not results:
      bot.reply_to(message, '❌ لم يتم العثور على أي طالب مطابق للبحث.')
    else:
      markup = types.InlineKeyboardMarkup()
      for tid, name, phone, status, created_at in results:
        btn = types.InlineKeyboardButton(
            text=f'📁 {name} ({phone})', callback_data=f'view_file_{tid}'
        )
        markup.add(btn)
      bot.reply_to(
          message,
          f'🔍 **نتائج البحث عن ({query}):**\nاضغط على الطالب للفتح:',
          reply_markup=markup,
      )

    del admin_input_states[message.from_user.id]

  elif state == 'awaiting_reset':
    conn = sqlite3.connect('freshmen_students.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE freshmen SET status = 'reset' WHERE phone = ? OR telegram_id ="
        ' ?',
        (query, query),
    )

    if cursor.rowcount > 0:
      conn.commit()
      bot.reply_to(message, f'✅ تم فك القفل عن حساب الطالب ({query}) بنجاح.')
    else:
      bot.reply_to(message, '❌ لم يتم العثور على الطالب.')
    conn.close()

    del admin_input_states[message.from_user.id]


# ==================== بدء التسجيل للمستجدين (/start) ====================
@bot.message_handler(commands=['start'])
def start_freshman(message):
  user_id = message.from_user.id

  if user_id == ADMIN_ID:
    admin_command(message)
    return

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


# ==================== خطوات إدخال البيانات والصور من الطالب ====================
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
  id_photo_id = message.photo[-1].file_id

  user_data = user_sessions[user_id]
  full_name = user_data['name']
  phone = user_data['phone']
  admission_photo_id = user_data['admission_photo']

  # 1. حفظ الطلب والمستندات في قاعدة البيانات
  conn = sqlite3.connect('freshmen_students.db')
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO freshmen (telegram_id, full_name, phone, admission_photo_id, id_photo_id, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        ON CONFLICT(telegram_id) DO UPDATE SET 
            full_name = excluded.full_name, 
            phone = excluded.phone, 
            admission_photo_id = excluded.admission_photo_id, 
            id_photo_id = excluded.id_photo_id, 
            status = 'pending'
    """,
      (user_id, full_name, phone, admission_photo_id, id_photo_id),
  )
  conn.commit()
  conn.close()

  # 2. إعلام الطالب بالاستلام
  bot.send_message(
      message.chat.id,
      '🚀 **تم استلام بياناتك وأوراقك بنجاح!**\n\n'
      'طلبك الآن قيد المراجعة والتدقيق، وسيصلك إشعار بالقبول مع روابط القنوات'
      ' فور الاعتماد.',
      parse_mode='Markdown',
  )

  # 3. تحويل الصور والبيانات المباشرة لشات الأدمن الشخصي (ADMIN_ID)
  try:
    # إرسال الصورة الأولى (المفاضلة)
    bot.send_photo(
        ADMIN_ID,
        admission_photo_id,
        caption=(
            f'📥 **طلب توثيق مستجد جديد (1/2 - المفاضلة):**\n\n'
            f'👤 **الاسم:** {full_name}\n'
            f'📱 **الهاتف:** `{phone}`\n'
            f'🆔 **Telegram ID:** `{user_id}`\n'
            f'👤 **المعرف:** @{message.from_user.username if message.from_user.username else "لا يوجد"}'
        ),
        parse_mode='Markdown',
    )

    # إرسال الصورة الثانية (الهوية) مع أزرار القبول والرفض المباشرة
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_approve = types.InlineKeyboardButton(
        text='✅ قبول وتوليد الرابط', callback_data=f'approve_{user_id}'
    )
    btn_reject = types.InlineKeyboardButton(
        text='❌ رفض الطلب', callback_data=f'reject_menu_{user_id}'
    )
    markup.add(btn_approve, btn_reject)

    bot.send_photo(
        ADMIN_ID,
        id_photo_id,
        caption=(
            f'🪪 **(2/2 - صورة الهوية) للطالب:** {full_name}\n👇 **اتخذ القرار'
            ' بضغطة زر:**'
        ),
        reply_markup=markup,
    )

  except Exception as e:
    bot.send_message(ADMIN_ID, f'⚠️ خطأ في تحويل أوراق الطالب: `{str(e)}`')

  if user_id in user_sessions:
    del user_sessions[user_id]


# ==================== معالجة القبول والرفض التلقائي بضغطة زر ====================
@bot.callback_query_handler(
    func=lambda call: call.data.startswith(
        ('approve_', 'reject_', 'retry_freshman')
    )
)
def handle_admin_decision(call):
  data = call.data

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
      bot.answer_callback_query(call.id, '❌ لم يتم العثور على الطالب.')
      conn.close()
      return

    full_name, phone = st

    try:
      # توليد رابطين شخصيين للكلية (استخدام شخصي لمرة واحدة)
      lectures_link = bot.create_chat_invite_link(
          chat_id=FRESHMAN_LECTURES_ID, member_limit=1
      ).invite_link
      discussion_link = bot.create_chat_invite_link(
          chat_id=FRESHMAN_DISCUSSION_ID, member_limit=1
      ).invite_link

      cursor.execute(
          "UPDATE freshmen SET status = 'approved' WHERE telegram_id = ?",
          (target_user_id,),
      )
      conn.commit()

      # إرسال الرسالة والروابط للطالب مباشرة تلقائياً!
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
          f'🎓 صنع هذا البوت @Youssef_Sabra.\n'
          f'👇 **إليك روابط الانضمام الخاصة بدفعتك (سنة أولى مستجدين):**'
      )
      bot.send_message(
          target_user_id, success_msg, reply_markup=markup, parse_mode='Markdown'
      )

      bot.edit_message_caption(
          f'✅ **تم قبول الطالب ({full_name} - {phone}) بنجاح وإرسال الروابط له'
          ' أوتوماتيكياً!**',
          chat_id=call.message.chat.id,
          message_id=call.message.message_id,
      )

    except Exception as e:
      bot.send_message(
          call.message.chat.id,
          f'❌ **تعذر توليد الروابط!**\nتأكد أن البوت مشرف في القناتين:'
          f' `{str(e)}`',
      )

    conn.close()

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
    bot.edit_message_caption(
        '📌 **اختر سبب رفض الطلب ليتم إبلاغ الطالب تلقائياً:**',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
    )

  elif data.startswith('reject_reason_'):
    parts = data.split('_')
    target_user_id = int(parts[2])
    reason_code = parts[3]

    reasons = {
        '1': '📷 الصور المرفقة غير واضحة، يرجى إعادة التصوير بشكل جلي والإرسال مجدداً.',
        '2': '📄 بطاقة المفاضلة المرفقة لا توضح القبول في قسم الهندسة الإلكترونية والاتصالات.',
        '3': '📄 صورة الهوية الشخصية غير واضحة أو غير مطابقة للبيانات.',
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
        f'👇 **يمكنك إعادة المحاولة بضغطة زر:**',
        reply_markup=markup,
        parse_mode='Markdown',
    )

    bot.edit_message_caption(
        f'❌ **تم رفض الطلب.**\nالسبب: {selected_reason}',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )


# ==================== التشغيل ====================
def run_bot():
  bot.infinity_polling(skip_pending=True)


if __name__ == '__main__':
  bot_thread = Thread(target=run_bot)
  bot_thread.daemon = True
  bot_thread.start()

  run_flask()

