import sqlite3
import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ===================== 配置区 =====================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8650104044:AAFdQFH_kgEEC1fo0ohj2eNDsj-mBqywLH8")
DB = "attendance.db"
WORK_START = "09:00"   # 标准上班时间
WORK_END = "21:00"     # 标准下班时间
MEAL_LIMIT = 30        # 单次吃饭上限(分钟)
TOILET_LIMIT = 10      # 单次厕所上限(分钟)
MAX_MEALS = 2          # 每日建议吃饭次数（超出只警告，不拦截）
MAX_TOILETS = 1        # 每日建议厕所次数（超出只警告，不拦截）
ADMIN_FILE = "admins.txt"

# 初始管理员ID（首次运行自动保存）
ADMIN_IDS = set()
# 员工白名单（空=不限制）
ALLOWED_USERS = set()

# 欢迎语
WELCOME_TEXT = """👋 欢迎使用员工考勤系统！

请直接点击下方按钮进行打卡：
🟢 上班打卡 / 🔴 下班打卡
🍚 吃饭、🚻 厕所需离座时点击
💺 返回座位时记得点回座

📌 上班时间：09:00  下班时间：21:00
📌 提示：首次使用直接点「🟢 上班打卡」即可开始考勤"""

TZ = timezone(timedelta(hours=8))
# =================================================================

def load_admins():
    path = Path(ADMIN_FILE)
    if not path.exists():
        if ADMIN_IDS:
            path.write_text("\n".join(str(x) for x in ADMIN_IDS))
        return ADMIN_IDS.copy()
    try:
        return set(int(x.strip()) for x in path.read_text().strip().splitlines() if x.strip().isdigit())
    except:
        return set()

def save_admins(admins):
    Path(ADMIN_FILE).write_text("\n".join(str(x) for x in admins))

ADMINS = load_admins()

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT,
    chat_id INTEGER,day TEXT,action TEXT,ts TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,name TEXT,first_seen TEXT)")
    c.commit()
    return c

def today(): return datetime.now(TZ).strftime("%Y-%m-%d")
def now(): return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def fmt_minutes(m):
    """总分钟数 → X小时X分钟（显示用）"""
    if m <= 0: return "0分钟"
    h = m // 60
    mm = m % 60
    return f"{h}小时{mm}分钟" if h > 0 else f"{mm}分钟"

def admin(uid): return uid in ADMINS

def is_new_user(uid, name):
    c = db()
    r = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r:
        c.execute("INSERT INTO users VALUES(?,?,?)", (uid, name, now()))
        c.commit()
        c.close()
        return True
    c.close()
    return False

def keyboard(uid):
    x = [["🟢 上班打卡","🔴 下班打卡"],["🍚 吃饭","🚻 厕所"],["💺 回座","📊 我的考勤"]]
    if admin(uid):
        x += [["📋 今日全员","📆 昨日全员"],["📊 统计汇总","📥 导出Excel"]]
    return ReplyKeyboardMarkup(x, resize_keyboard=True)

def rows(uid, day):
    c = db()
    r = c.execute("SELECT user_id,name,action,ts FROM logs WHERE user_id=? AND day=? ORDER BY id",(uid, day)).fetchall()
    c.close()
    return r

def allrows(day):
    c = db()
    r = c.execute("SELECT user_id,name,action,ts FROM logs WHERE day=? ORDER BY name,id",(day,)).fetchall()
    c.close()
    return r

def state(r):
    s = None
    for x in r:
        if x[2] in ("吃饭","厕所"): s = (x[2], x[3])
        elif x[2] == "回座": s = None
    return s

def intervals(r, act):
    out = []
    for i, x in enumerate(r):
        if x[2] != act: continue
        back = next((z[3] for z in r[i+1:] if z[2]=="回座"), None)
        if back:
            fmt = "%Y-%m-%d %H:%M:%S"
            t1 = datetime.strptime(x[3], fmt)
            t2 = datetime.strptime(back, fmt)
            out.append(int((t2 - t1).total_seconds() // 60))
    return out

# ========== 核心修复：精确计算迟到/早退分钟 ==========

def calc_late_minutes(day, checkin_time):
    """
    计算迟到分钟：
    标准上班 = day + 09:00:00
    打卡晚于标准 = 迟到分钟（精确到分）
    打卡早于/等于标准 = 0分钟
    """
    if not checkin_time:
        return 0
    fmt_full = "%Y-%m-%d %H:%M:%S"
    fmt_time = "%Y-%m-%d %H:%M"
    
    try:
        std_time_str = f"{day} {WORK_START}:00"
        std_dt = datetime.strptime(std_time_str, fmt_full)
        
        # 解析打卡时间（兼容带秒/不带秒）
        if len(checkin_time) == 19:
            check_dt = datetime.strptime(checkin_time, fmt_full)
        else:
            check_dt = datetime.strptime(checkin_time[:16], fmt_time)
        
        # 打卡时间 - 标准时间 = 迟到分钟
        diff_minutes = int((check_dt - std_dt).total_seconds() // 60)
        return max(0, diff_minutes)  # 早到=0, 晚到=正数
    except Exception as e:
        print(f"计算迟到出错: {e}")
        return 0

def calc_early_minutes(day, checkout_time):
    """
    计算早退分钟：
    标准下班 = day + 21:00:00
    打卡早于标准 = 早退分钟
    打卡晚于/等于标准 = 0分钟
    """
    if not checkout_time:
        return 0
    fmt_full = "%Y-%m-%d %H:%M:%S"
    fmt_time = "%Y-%m-%d %H:%M"
    
    try:
        std_time_str = f"{day} {WORK_END}:00"
        std_dt = datetime.strptime(std_time_str, fmt_full)
        
        if len(checkout_time) == 19:
            check_dt = datetime.strptime(checkout_time, fmt_full)
        else:
            check_dt = datetime.strptime(checkout_time[:16], fmt_time)
        
        # 标准时间 - 打卡时间 = 早退分钟
        diff_minutes = int((std_dt - check_dt).total_seconds() // 60)
        return max(0, diff_minutes)
    except Exception as e:
        print(f"计算早退出错: {e}")
        return 0

def fmt_late_display(minutes):
    """迟到显示文本"""
    if minutes == 0:
        return "✅ 正常（未迟到）"
    return f"⚠️ 迟到 {minutes} 分钟（{fmt_minutes(minutes)}）"

def fmt_early_display(minutes):
    """早退显示文本"""
    if minutes == 0:
        return "✅ 正常（未早退）"
    return f"⚠️ 早退 {minutes} 分钟（{fmt_minutes(minutes)}）"

def summary(uid, day):
    r = rows(uid, day)
    if not r: return None
    name = r[0][1]
    w = next((x[3] for x in r if x[2]=="上班"), None)
    o = next((x[3] for x in reversed(r) if x[2]=="下班"), None)
    m = intervals(r, "吃饭")
    t = intervals(r, "厕所")
    late = calc_late_minutes(day, w)
    early = calc_early_minutes(day, o)
    
    # 计算在岗时长（分钟）
    total_on = 0
    if w and o:
        fmt_full = "%Y-%m-%d %H:%M:%S"
        t1 = datetime.strptime(w, fmt_full)
        t2 = datetime.strptime(o, fmt_full)
        total_on = max(0, int((t2 - t1).total_seconds() // 60))
    
    away = sum(m) + sum(t)
    work_net = max(0, total_on - away)
    meal_over_cnt = max(0, len(m) - MAX_MEALS)
    toilet_over_cnt = max(0, len(t) - MAX_TOILETS)
    return name, w, o, late, early, m, t, total_on, work_net, meal_over_cnt, toilet_over_cnt

def text_summary(uid, day):
    s = summary(uid, day)
    if not s: return "暂无记录"
    n, w, o, late, early, m, t, total_on, work_net, meal_over_cnt, toilet_over_cnt = s
    q = [f"👤 用户：{n}｜用户标识：{uid}｜{day}",
         f"🟢 上班：{w[11:16] if w else '未打卡'}"]
    q.append(f"⏰ 迟到：{fmt_late_display(late)}")
    q.append(f"🔴 下班：{o[11:16] if o else '未打卡'}")
    q.append(f"⏰ 早退：{fmt_early_display(early)}")
    if w and o:
        q.append(f"⏰ 当日在岗：{fmt_minutes(total_on)}")
        q.append(f"💼 纯工作时长：{fmt_minutes(work_net)}")
    
    meal_line = f"🍚 吃饭：{len(m)}次 / 累计{sum(m)}分钟"
    if any(x>MEAL_LIMIT for x in m): meal_line += " ⚠️单次超时"
    if meal_over_cnt > 0: meal_line += f" ⚠️超出建议{MAX_MEALS}次，多{meal_over_cnt}次"
    q.append(meal_line)
    
    toilet_line = f"🚻 厕所：{len(t)}次 / 累计{sum(t)}分钟"
    if any(x>TOILET_LIMIT for x in t): toilet_line += " ⚠️单次超时"
    if toilet_over_cnt > 0: toilet_line += f" ⚠️超出建议{MAX_TOILETS}次，多{toilet_over_cnt}次"
    q.append(toilet_line)
    
    q.append(f"⏱️ 今日离岗累计：{sum(m)+sum(t)}分钟")
    return "\n".join(q)

def people(day):
    seen = []; out = []
    for x in allrows(day):
        if x[0] not in seen: seen.append(x[0]); out.append(x[0])
    return out

def export_xlsx(day):
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    wb = Workbook(); ws = wb.active; ws.title = "考勤汇总"
    h = ["日期","姓名","用户标识","上班时间","下班时间","迟到分钟","早退分钟","当日在岗","纯工作时长",
         "吃饭次数","吃饭超限次数","吃饭总分钟","吃饭超时",
         "厕所次数","厕所超限次数","厕所总分钟","厕所超时","离岗累计"]
    ws.append(h)
    for uid in people(day):
        s = summary(uid, day)
        if not s: continue
        n, w, o, late, early, m, t, total_on, work_net, meal_over_cnt, toilet_over_cnt = s
        ws.append([
            day, n, uid,
            w[11:16] if w else "",
            o[11:16] if o else "",
            late, early,  # 直接导出分钟数字，方便筛选
            fmt_minutes(total_on) if w and o else "",
            fmt_minutes(work_net) if w and o else "",
            len(m), meal_over_cnt, sum(m),
            "是" if any(x>MEAL_LIMIT for x in m) else "否",
            len(t), toilet_over_cnt, sum(t),
            "是" if any(x>TOILET_LIMIT for x in t) else "否",
            sum(m)+sum(t)
        ])
    for i in range(1, len(h)+1): ws.column_dimensions[get_column_letter(i)].width = 16
    p = f"考勤_{day}.xlsx"; wb.save(p); return p

def force_close_previous(uid, name, cid, today_str, db_conn):
    yday = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    r = rows(uid, yday)
    if not r: return False
    lw = next((x[3] for x in r if x[2]=="上班"), None)
    lo = next((x[3] for x in reversed(r) if x[2]=="下班"), None)
    if lw and (not lo or lo < lw):
        fake_end = f"{yday} {WORK_END}:00"
        db_conn.execute("INSERT INTO logs(user_id,name,chat_id,day,action,ts) VALUES(?,?,?,?,?,?)",
                        (uid, name, cid, yday, "下班", fake_end))
        db_conn.commit()
        return True
    return False

async def start(u, c):
    uid = u.effective_user.id
    name = u.effective_user.full_name or f"用户{uid}"
    if is_new_user(uid, name):
        await u.message.reply_text(f"🎉 新用户「{name}」已注册！\n\n{WELCOME_TEXT}", reply_markup=keyboard(uid))
    else:
        await u.message.reply_text(f"👋 欢迎回来，{name}！\n请选择下方操作：", reply_markup=keyboard(uid))

async def make_admin(u, c):
    uid = u.effective_user.id
    if not Path(ADMIN_FILE).exists() and not ADMINS:
        ADMINS.add(uid); save_admins(ADMINS)
        await u.message.reply_text("👑 你已成为第一位管理员。\n可用 /addadmin 用户ID  添加更多管理员。", reply_markup=keyboard(uid))
    elif admin(uid):
        await u.message.reply_text(f"👑 你是管理员。\n当前管理员：{', '.join(map(str,ADMINS))}", reply_markup=keyboard(uid))
    else:
        await u.message.reply_text("⛔ 你不是管理员。")

async def add_admin_cmd(u, c):
    uid = u.effective_user.id
    if not admin(uid):
        await u.message.reply_text("⛔ 只有管理员可以添加。")
        return
    args = u.message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await u.message.reply_text("⚠️ 格式：/addadmin 用户数字ID\n例如：/addadmin 123456789")
        return
    new_id = int(args[1])
    if new_id in ADMINS:
        await u.message.reply_text("⚠️ 该用户已经是管理员。")
        return
    ADMINS.add(new_id); save_admins(ADMINS)
    await u.message.reply_text(f"✅ 已添加管理员：{new_id}\n当前列表：{', '.join(map(str,ADMINS))}")

async def del_admin_cmd(u, c):
    uid = u.effective_user.id
    if not admin(uid):
        await u.message.reply_text("⛔ 只有管理员可以移除。")
        return
    args = u.message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await u.message.reply_text("⚠️ 格式：/deladmin 用户数字ID\n例如：/deladmin 123456789")
        return
    del_id = int(args[1])
    if del_id not in ADMINS:
        await u.message.reply_text("⚠️ 该用户不是管理员。")
        return
    if del_id == uid:
        await u.message.reply_text("⚠️ 不能移除自己。")
        return
    ADMINS.remove(del_id); save_admins(ADMINS)
    await u.message.reply_text(f"✅ 已移除管理员：{del_id}\n当前列表：{', '.join(map(str,ADMINS))}")

async def handle(u, c):
    tx = u.message.text; uid = u.effective_user.id
    name = u.effective_user.full_name or f"用户{uid}"
    cid = u.effective_chat.id; day = today()

    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await u.message.reply_text("⛔ 仅限内部员工使用。")
        return

    r = rows(uid, day); cn = db()
    def last(a):
        for x in reversed(r):
            if x[2] == a: return x[3]
    st = state(r)

    # ─── 上班打卡 ───
    if tx == "🟢 上班打卡":
        forced = force_close_previous(uid, name, cid, day, cn)
        lw = last("上班"); lo = last("下班")
        if lw and (not lo or lw > lo):
            msg = f"👤 用户：{name}｜用户标识：{uid}\n⚠️ 当前已是上班状态，不能重复打卡。"
        else:
            ts = now()
            cn.execute("INSERT INTO logs VALUES(NULL,?,?,?,?,?,?)",
                      (uid, name, cid, day, "上班", ts))
            cn.commit()
            late = calc_late_minutes(day, ts)
            msg = (f"👤 用户：{name}｜用户标识：{uid}\n🟢 上班打卡成功\n"
                   f"时间：{ts[11:16]}\n⏰ 迟到：{fmt_late_display(late)}")
            if forced: msg += "\n✅ 昨日未下班，已自动补下班。"

    # ─── 下班打卡 ───
    elif tx == "🔴 下班打卡":
        lw = last("上班"); lo = last("下班")
        if not lw or (lo and lo >= lw):
            msg = f"👤 用户：{name}｜用户标识：{uid}\n⚠️ 请先上班打卡。"
        elif st:
            msg = f"👤 用户：{name}｜用户标识：{uid}\n⚠️ 你还在离座中，请先回座。"
        else:
            ts = now()
            cn.execute("INSERT INTO logs VALUES(NULL,?,?,?,?,?,?)",
                      (uid, name, cid, day, "下班", ts))
            cn.commit()
            early = calc_early_minutes(day, ts)
            fmt_full = "%Y-%m-%d %H:%M:%S"
            t1 = datetime.strptime(lw, fmt_full)
            t2 = datetime.strptime(ts, fmt_full)
            total_on = max(0, int((t2 - t1).total_seconds() // 60))
            m = intervals(r, "吃饭"); t = intervals(r, "厕所")
            work_net = max(0, total_on - sum(m) - sum(t))
            msg = (f"👤 用户：{name}｜用户标识：{uid}\n🔴 下班打卡成功\n"
                   f"时间：{ts[11:16]}\n⏰ 早退：{fmt_early_display(early)}\n"
                   f"⏰ 当日在岗：{fmt_minutes(total_on)}\n💼 纯工作时长：{fmt_minutes(work_net)}")

    # ─── 吃饭 / 厕所 ───
    elif tx in ("🍚 吃饭","🚻 厕所"):
        lw = last("上班"); lo = last("下班")
        if not lw or (lo and lo >= lw):
            msg = f"👤 用户：{name}｜用户标识：{uid}\n⚠️ 请先上班打卡。"
        elif st:
            msg = f"👤 用户：{name}｜用户标识：{uid}\n⚠️ 当前已在离座，请先回座。"
        else:
            a = "吃饭" if tx=="🍚 吃饭" else "厕所"
            mx = MAX_MEALS if a=="吃饭" else MAX_TOILETS
            count = sum(x[2]==a for x in r) + 1
            ts = now()
            cn.execute("INSERT INTO logs VALUES(NULL,?,?,?,?,?,?)",
                      (uid, name, cid, day, a, ts))
            cn.commit()
            msg = (f"👤 用户：{name}｜用户标识：{uid}\n{tx} 已开始计时。\n"
                   f"📌 今日第{count}次{a}\n回来后点击「💺 回座」。")
            if count > mx:
                msg += f"\n⚠️ 注意：今日{a}已超出建议{mx}次，本次是第{count}次，已记录统计！"

    # ─── 回座 ───
    elif tx == "💺 回座":
        if not st:
            msg = f"👤 用户：{name}｜用户标识：{uid}\n⚠️ 当前没有进行中的离座记录。"
        else:
            a, start_ts = st; end_ts = now()
            fmt_full = "%Y-%m-%d %H:%M:%S"
            used = int((datetime.strptime(end_ts, fmt_full) - datetime.strptime(start_ts, fmt_full)).total_seconds() // 60)
            limit = MEAL_LIMIT if a=="吃饭" else TOILET_LIMIT
            r_all = rows(uid, day)
            items = intervals(r_all, a)
            total = sum(items) + used
            idx = len(items) + 1
            all_meal = sum(intervals(r_all, "吃饭"))
            all_toilet = sum(intervals(r_all, "厕所"))
            all_total = all_meal + all_toilet + used
            mx = MAX_MEALS if a=="吃饭" else MAX_TOILETS

            cn.execute("INSERT INTO logs VALUES(NULL,?,?,?,?,?,?)",
                      (uid, name, cid, day, "回座", end_ts))
            cn.commit()

            msg = (f"👤 用户：{name}｜用户标识：{uid}\n💺 回座成功\n"
                   f"{a}：本次 {used} 分钟\n📌 今日第 {idx} 次{a}｜累计 {total} 分钟\n")
            if idx > mx:
                msg += f"⚠️ 超出建议次数：建议{mx}次，实际{idx}次，超出{idx-mx}次\n"
            msg += (f"🍚 吃饭累计：{all_meal + (used if a=='吃饭' else 0)} 分钟\n"
                    f"🚻 厕所累计：{all_toilet + (used if a=='厕所' else 0)} 分钟\n"
                    f"⏱️ 今日离岗累计：{all_total} 分钟\n")
            if used > limit:
                msg += f"⚠️ 超时 {used - limit} 分钟"
            else:
                msg += "✅ 未超时"

    # ─── 我的考勤 ───
    elif tx == "📊 我的考勤":
        msg = text_summary(uid, day)

    # ─── 管理员功能 ───
    elif tx in ("📋 今日全员","📆 昨日全员","📊 统计汇总","📥 导出Excel"):
        if not admin(uid):
            msg = "⛔ 仅管理员可用。"
        elif tx == "📋 今日全员":
            ss = [summary(x, day) for x in people(day)]
            msg = "📋 今日全员考勤\n\n" + "\n\n".join(text_summary(x[0], day) for x in ss) if ss else "暂无记录"
        elif tx == "📆 昨日全员":
            d = (datetime.strptime(day,"%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            ss = [summary(x, d) for x in people(d)]
            msg = f"📆 昨日全员｜{d}\n\n" + "\n\n".join(text_summary(x[0], d) for x in ss) if ss else "暂无记录"
        elif tx == "📊 统计汇总":
            ss = [summary(x, day) for x in people(day)]
            late_count = sum(x[3]>0 for x in ss)
            early_count = sum(x[4]>0 for x in ss)
            meal_over_users = sum(x[10]>0 for x in ss)
            toilet_over_users = sum(x[11]>0 for x in ss)
            mo = sum(any(v>MEAL_LIMIT for v in x[5]) for x in ss)
            to = sum(any(v>TOILET_LIMIT for v in x[6]) for x in ss)
            msg = (f"📊 今日统计｜{day}\n👥 打卡人数：{len(ss)}人\n"
                   f"⚠️ 迟到：{late_count}人\n⚠️ 早退：{early_count}人\n"
                   f"🍚 吃饭次数超限：{meal_over_users}人\n🚻 厕所次数超限：{toilet_over_users}人\n"
                   f"⏰ 吃饭时长超时：{mo}人\n🚻 厕所时长超时：{to}人")
        else:
            try:
                p = export_xlsx(day)
                await u.message.reply_document(open(p,"rb"), caption=f"📥 {day}考勤Excel")
                Path(p).unlink(missing_ok=True)
                cn.close(); return
            except ImportError:
                msg = "⚠️ 缺少Excel组件，请先安装 openpyxl。"
    else:
        msg = f"👤 用户：{name}｜用户标识：{uid}\n请选择下方按钮操作。"

    cn.close()
    await u.message.reply_text(msg, reply_markup=keyboard(uid))

async def make_admin_cmd(u, c):
    await make_admin(u, c)


async def error_handler(update, context):
    """Silently handle Conflict errors (duplicate bot instances); log others."""
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        logging.getLogger(__name__).info('Telegram Conflict: another instance running. Retrying...')
        return
    if isinstance(err, NetworkError):
        logging.getLogger(__name__).warning('Network error: %s', err)
        return
    logging.getLogger(__name__).error('Unhandled error:', exc_info=err)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", make_admin_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("deladmin", del_admin_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_error_handler(error_handler)
    print("✅ 考勤机器人已启动，保持窗口运行即可。")
    app.run_polling()

if __name__ == "__main__":
    main()
