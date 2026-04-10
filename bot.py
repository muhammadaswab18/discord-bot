import os
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
TZ = ZoneInfo(os.getenv("TZ", "Asia/Karachi"))

MORNING_HOUR = int(os.getenv("MORNING_HOUR", "10"))
MORNING_MINUTE = int(os.getenv("MORNING_MINUTE", "0"))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "18"))
EVENING_MINUTE = int(os.getenv("EVENING_MINUTE", "45"))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

session_state = {
    "date": None,
    "morning": {
        "active": False,
        "session_message_id": None,
        "thread_id": None,
        "status_message_id": None,
        "replied_users": set(),
        "user_steps": {}
    },
    "evening": {
        "active": False,
        "session_message_id": None,
        "thread_id": None,
        "status_message_id": None,
        "replied_users": set(),
        "user_steps": {}
    },
}

HEADERS = [
    "Date",
    "User ID",
    "Username",
    "Display Name",
    "Morning Working On",
    "Morning Blockers / Additional Note",
    "Morning Timestamp",
    "Evening Achieved",
    "Evening Pending / Blockers",
    "Evening Timestamp",
]

def get_gsheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    if GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif GOOGLE_SERVICE_ACCOUNT_FILE:
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=scopes
        )
    else:
        raise ValueError(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE."
        )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    return sheet

def ensure_sheet_headers():
    sheet = get_gsheet()
    first_row = sheet.row_values(1)
    if first_row != HEADERS:
        if not first_row:
            sheet.append_row(HEADERS)
        else:
            sheet.update(range_name="A1:J1", values=[HEADERS])

def find_row(sheet, date_str, user_id):
    records = sheet.get_all_values()
    for idx, row in enumerate(records[1:], start=2):
        row_date = row[0] if len(row) > 0 else ""
        row_user_id = row[1] if len(row) > 1 else ""
        if str(row_date) == str(date_str) and str(row_user_id) == str(user_id):
            return idx
    return None

def save_update(date_str, user, update_type, part1, part2, timestamp_str):
    sheet = get_gsheet()
    row_num = find_row(sheet, date_str, user.id)

    if row_num is None:
        new_row = [
            date_str,
            str(user.id),
            str(user),
            getattr(user, "display_name", str(user)),
            "", "", "", "", "", ""
        ]
        sheet.append_row(new_row)
        row_num = find_row(sheet, date_str, user.id)

    if update_type == "morning":
        sheet.update(f"E{row_num}:G{row_num}", [[part1, part2, timestamp_str]])
    elif update_type == "evening":
        sheet.update(f"H{row_num}:J{row_num}", [[part1, part2, timestamp_str]])

def get_now():
    return datetime.now(TZ)

def get_today_str():
    return get_now().strftime("%Y-%m-%d")

def get_target_members(guild):
    return [member for member in guild.members if not member.bot]


def get_human_date():
    return get_now().strftime("%b %d, %Y")

def reset_for_new_day():
    today = get_today_str()
    if session_state["date"] != today:
        old_date = session_state["date"]
        print(f"[TIME] Date rollover detected: old_date={old_date}, new_date={today}")
        session_state["date"] = today
        session_state["morning"] = {
            "active": False,
            "session_message_id": None,
            "thread_id": None,
            "status_message_id": None,
            "replied_users": set(),
            "user_steps": {}
        }
        session_state["evening"] = {
            "active": False,
            "session_message_id": None,
            "thread_id": None,
            "status_message_id": None,
            "replied_users": set(),
            "user_steps": {}
        }

async def update_status_message(channel, guild, session_type):
    state = session_state[session_type]
    status_message_id = state["status_message_id"]

    if not status_message_id:
        return

    try:
        status_message = await channel.fetch_message(status_message_id)
    except discord.NotFound:
        return

    total_users = len(get_target_members(guild))
    replied = len(state["replied_users"])
    pending = max(total_users - replied, 0)

    plan_label = "Standup 1 - Daily Plan" if session_type == "morning" else "Standup 2 - Daily Results"
    content = (
        f"**{plan_label}**\n"
        f"Reported: **{replied} out of {total_users}**\n"
        f"Pending: **{pending}**"
    )

    await status_message.edit(content=content)

async def start_session(session_type):
    print(f"[SESSION] start_session called with session_type={session_type}")
    reset_for_new_day()

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[SESSION] Channel not found for CHANNEL_ID={CHANNEL_ID}.")
        return
    print(f"[SESSION] Channel found: channel_id={channel.id}")

    guild = channel.guild
    if guild is None:
        print("[SESSION] Guild not found for resolved channel.")
        return
    print(f"[SESSION] Using guild: name={guild.name}, id={guild.id}")

    state = session_state[session_type]
    state["active"] = True
    state["replied_users"].clear()
    state["user_steps"].clear()

    if session_type == "morning":
        title = f"Standup 1 - Daily Plan, {get_human_date()}"
        session_text = (
            f"🧠 **{title}**\n\n"
            f"Find all reports for **{title}** in the thread. 🧵"
        )
    else:
        title = f"Standup 2 - Daily Results, {get_human_date()}"
        session_text = (
            f"🌙 **{title}**\n\n"
            f"Find all reports for **{title}** in the thread. 🧵"
        )

    session_message = await channel.send(session_text)
    print(f"[SESSION] Session message sent for {session_type}.")
    thread_name = f"{session_type}-updates-{get_today_str()}"
    session_thread = await session_message.create_thread(name=thread_name)
    print(f"[SESSION] Session thread created: thread_id={session_thread.id}")
    await session_thread.send(
        f"Use this thread for {session_type} updates.\n"
        "Send any message to start."
    )
    status_message = await channel.send("Preparing status...")
    print(f"[SESSION] Status message sent for {session_type}.")

    state["session_message_id"] = session_message.id
    state["thread_id"] = session_thread.id
    state["status_message_id"] = status_message.id
    print(
        f"[SESSION] Saved message IDs for {session_type}: "
        f"session_message_id={session_message.id}, thread_id={session_thread.id}, "
        f"status_message_id={status_message.id}"
    )

    await update_status_message(channel, guild, session_type)

def get_active_session_type():
    if session_state["morning"]["active"]:
        return "morning"
    if session_state["evening"]["active"]:
        return "evening"
    return None

@bot.event
async def on_ready():
    print(f"[READY] Logged in as {bot.user}")
    print(f"[TIME] Configured timezone={TZ}, current_local_time={get_now().isoformat()}")
    print("[READY] Calling ensure_sheet_headers()")
    try:
        ensure_sheet_headers()
        print("[READY] ensure_sheet_headers() completed successfully")
    except Exception as e:
        print(f"[SHEET] ensure_sheet_headers() failed with exception: {e!r}")

    if not scheduler.is_running():
        print("[READY] scheduler.start() will be called")
        scheduler.start()
    else:
        print("[READY] Scheduler already running")

    for guild in bot.guilds:
        print(f"[READY] Chunking guild: name={guild.name}, id={guild.id}")
        try:
            await asyncio.wait_for(guild.chunk(), timeout=20)
        except asyncio.TimeoutError:
            print(f"[READY] Guild chunk timed out for guild {guild.name} ({guild.id})")
        except Exception as e:
            print(f"[READY] Could not chunk guild {guild.name} ({guild.id}): {e!r}")
        else:
            print(f"[READY] Successfully chunked guild: name={guild.name}, id={guild.id}")

@tasks.loop(minutes=1)
async def scheduler():
    reset_for_new_day()
    now = get_now()
    print(
        "[SCHEDULER] Tick: "
        f"now={now.isoformat()}, "
        f"morning={MORNING_HOUR:02d}:{MORNING_MINUTE:02d}, "
        f"evening={EVENING_HOUR:02d}:{EVENING_MINUTE:02d}"
    )

    if now.hour == MORNING_HOUR and now.minute == MORNING_MINUTE:
        print("[SCHEDULER] Morning condition matched")
        if not session_state["morning"]["active"]:
            await start_session("morning")
        else:
            print("[SCHEDULER] Morning session skipped; already active")

    if now.hour == EVENING_HOUR and now.minute == EVENING_MINUTE:
        print("[SCHEDULER] Evening condition matched")
        if not session_state["evening"]["active"]:
            await start_session("evening")
        else:
            print("[SCHEDULER] Evening session skipped; already active")

@bot.command()
async def closemorning(ctx):
    session_state["morning"]["active"] = False
    await ctx.send("Morning session closed.")

@bot.command()
async def closeevening(ctx):
    session_state["evening"]["active"] = False
    await ctx.send("Evening session closed.")

@bot.event
async def on_message(message):
    if message.author.bot:
        print(f"[MESSAGE] Ignored bot message: author_id={message.author.id}")
        return

    await bot.process_commands(message)

    is_main_channel = message.channel.id == CHANNEL_ID
    is_thread_message = isinstance(message.channel, discord.Thread)
    if not is_main_channel and not is_thread_message:
        print(
            f"[MESSAGE] Ignored message in wrong channel: "
            f"message_channel_id={message.channel.id}, target_channel_id={CHANNEL_ID}"
        )
        return
    print(f"[MESSAGE] Message received in target channel: author_id={message.author.id}")

    reset_for_new_day()

    guild = message.guild
    if guild is None:
        print("[MESSAGE] Ignored message: no guild (likely DM)")
        return

    active_session = get_active_session_type()
    if active_session is None:
        print("[MESSAGE] Ignored message: no active session")
        return
    print(f"[MESSAGE] Active session detected: session_type={active_session}")

    state = session_state[active_session]
    user_id = message.author.id

    if not state["active"] or user_id in state["replied_users"]:
        if not state["active"]:
            print(f"[MESSAGE] Ignored message: session not active for {active_session}")
        else:
            print(f"[MESSAGE] Ignored message: user already replied user_id={user_id}")
        return

    thread_id = state["thread_id"]
    if thread_id is None:
        print(f"[MESSAGE] Ignored message: no thread configured for {active_session}")
        return

    thread = bot.get_channel(thread_id)
    if thread is None:
        try:
            thread = await bot.fetch_channel(thread_id)
        except Exception as e:
            print(f"[MESSAGE] Could not fetch session thread: {e!r}")
            return

    if is_main_channel:
        await message.reply(f"Continue your update in thread: <#{thread_id}>")
        return

    if message.channel.id != thread_id:
        print(
            f"[MESSAGE] Ignored message in non-session thread: "
            f"thread_id={message.channel.id}, expected_thread_id={thread_id}"
        )
        return

    today_str = get_today_str()
    now_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    user_steps = state["user_steps"]

    if user_id not in user_steps:
        if active_session == "morning":
            bot_prompt = await thread.send(
                f"{message.author.mention} What will you be working on today?"
            )
        else:
            bot_prompt = await thread.send(f"{message.author.mention} What did you achieve today?")

        user_steps[user_id] = {
            "stage": "awaiting_part1",
            "part1": "",
            "part2": "",
            "last_bot_message_id": bot_prompt.id,
            "part1_message_id": None,
            "part2_message_id": None,
            "submitted": False
        }
        print(f"[MESSAGE] Stage transition: user_id={user_id}, stage=awaiting_part1")
        return

    step_data = user_steps[user_id]

    if step_data["stage"] == "awaiting_part1":
        step_data["part1"] = message.content.strip()
        step_data["part1_message_id"] = message.id
        if active_session == "morning":
            await thread.send(
                f"{message.author.mention} Any challenges or support you need to get started?"
            )
        else:
            await thread.send(f"{message.author.mention} Any obstacles that still need attention?")
        step_data["stage"] = "awaiting_part2"
        print(f"[MESSAGE] Stage transition: user_id={user_id}, stage=awaiting_part2")
        return

    if step_data["stage"] == "awaiting_part2":
        step_data["part2"] = message.content.strip()
        step_data["part2_message_id"] = message.id
        save_update(
            today_str,
            message.author,
            active_session,
            step_data["part1"],
            step_data["part2"],
            now_str
        )
        state["replied_users"].add(user_id)
        step_data["submitted"] = True

        if active_session == "morning":
            await thread.send(f"{message.author.mention} Great, good day ahead.")
        else:
            await thread.send(f"{message.author.mention} Great job.")

        main_channel = bot.get_channel(CHANNEL_ID)
        if main_channel is not None:
            await update_status_message(main_channel, guild, active_session)


@bot.event
async def on_message_edit(before, after):
    if after.author.bot:
        return
    if before.content == after.content:
        return
    if not isinstance(after.channel, discord.Thread):
        return

    active_session = get_active_session_type()
    if active_session is None:
        return

    state = session_state[active_session]
    if not state["active"] or after.channel.id != state.get("thread_id"):
        return

    user_id = after.author.id
    if user_id not in state["user_steps"]:
        return

    step_data = state["user_steps"][user_id]
    if not step_data.get("submitted"):
        return

    updated = False
    if after.id == step_data.get("part1_message_id"):
        step_data["part1"] = after.content.strip()
        updated = True
    elif after.id == step_data.get("part2_message_id"):
        step_data["part2"] = after.content.strip()
        updated = True

    if not updated:
        return

    save_update(
        get_today_str(),
        after.author,
        active_session,
        step_data.get("part1", ""),
        step_data.get("part2", ""),
        get_now().strftime("%Y-%m-%d %H:%M:%S")
    )

    await after.channel.send(f"{after.author.mention} updated. Sheet synced.")

bot.run(TOKEN)