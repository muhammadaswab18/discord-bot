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
        "status_message_id": None,
        "replied_users": set(),
        "user_steps": {}
    },
    "evening": {
        "active": False,
        "session_message_id": None,
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

def reset_for_new_day():
    today = get_today_str()
    if session_state["date"] != today:
        old_date = session_state["date"]
        print(f"[TIME] Date rollover detected: old_date={old_date}, new_date={today}")
        session_state["date"] = today
        session_state["morning"] = {
            "active": False,
            "session_message_id": None,
            "status_message_id": None,
            "replied_users": set(),
            "user_steps": {}
        }
        session_state["evening"] = {
            "active": False,
            "session_message_id": None,
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

    label = "Morning" if session_type == "morning" else "Evening"
    content = (
        f"**{label} Status**\n"
        f"Replied: **{replied}/{total_users}**\n"
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
        session_text = "🌞 **Morning Update Time**\nReply to this message to submit your update."
    else:
        session_text = "🌙 **Evening Update Time**\nReply to this message to submit your update."

    session_message = await channel.send(session_text)
    print(f"[SESSION] Session message sent for {session_type}.")
    status_message = await channel.send("Preparing status...")
    print(f"[SESSION] Status message sent for {session_type}.")

    state["session_message_id"] = session_message.id
    state["status_message_id"] = status_message.id
    print(
        f"[SESSION] Saved message IDs for {session_type}: "
        f"session_message_id={session_message.id}, status_message_id={status_message.id}"
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

    if message.channel.id != CHANNEL_ID:
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
    if message.reference is None:
        print("[MESSAGE] Ignored message: no reference")
        return

    try:
        referenced_message = await message.channel.fetch_message(message.reference.message_id)
    except Exception as e:
        print(f"[MESSAGE] Ignored message: failed to fetch referenced message: {e!r}")
        return

    user_id = message.author.id
    today_str = get_today_str()
    now_str = get_now().strftime("%Y-%m-%d %H:%M:%S")

    active_session = get_active_session_type()
    if active_session is None:
        print("[MESSAGE] Ignored message: no active session")
        return
    print(f"[MESSAGE] Active session detected: session_type={active_session}")

    state = session_state[active_session]

    if not state["active"] or user_id in state["replied_users"]:
        if not state["active"]:
            print(f"[MESSAGE] Ignored message: session not active for {active_session}")
        else:
            print(f"[MESSAGE] Ignored message: user already replied user_id={user_id}")
        return

    session_message_id = state["session_message_id"]
    user_steps = state["user_steps"]

    if referenced_message.id == session_message_id:
        if user_id in user_steps:
            print(f"[MESSAGE] Ignored message: user already in progress user_id={user_id}")
            return

        if active_session == "morning":
            bot_prompt = await message.reply("Working on")
        else:
            bot_prompt = await message.reply("What did you achieve today?")

        user_steps[user_id] = {
            "stage": "awaiting_part1",
            "part1": "",
            "part2": "",
            "last_bot_message_id": bot_prompt.id
        }
        print(
            f"[MESSAGE] Stage transition: user_id={user_id}, "
            f"stage=awaiting_part1, bot_prompt_id={bot_prompt.id}"
        )
        return

    if user_id not in user_steps:
        print(f"[MESSAGE] Ignored message: user has no active flow user_id={user_id}")
        return

    step_data = user_steps[user_id]

    if referenced_message.id != step_data["last_bot_message_id"]:
        print(
            f"[MESSAGE] Ignored message: wrong referenced message for user_id={user_id}, "
            f"expected={step_data['last_bot_message_id']}, got={referenced_message.id}"
        )
        return

    if step_data["stage"] == "awaiting_part1":
        step_data["part1"] = message.content.strip()

        if active_session == "morning":
            bot_prompt = await message.reply("Blockers / additional note")
        else:
            bot_prompt = await message.reply("Pending / blockers")

        step_data["stage"] = "awaiting_part2"
        step_data["last_bot_message_id"] = bot_prompt.id
        print(
            f"[MESSAGE] Stage transition: user_id={user_id}, "
            f"stage=awaiting_part2, bot_prompt_id={bot_prompt.id}"
        )
        return

    if step_data["stage"] == "awaiting_part2":
        step_data["part2"] = message.content.strip()

        save_update(
            today_str,
            message.author,
            active_session,
            step_data["part1"],
            step_data["part2"],
            now_str
        )
        print(
            f"[MESSAGE] Saved update successfully: "
            f"user_id={user_id}, session_type={active_session}, date={today_str}"
        )

        state["replied_users"].add(user_id)

        if active_session == "morning":
            await message.reply("Great")
        else:
            await message.reply("Great job")

        await update_status_message(message.channel, guild, active_session)

bot.run(TOKEN)