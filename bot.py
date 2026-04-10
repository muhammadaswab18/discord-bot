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
views_registered = False


def get_user_color(user_id):
    palette = [
        discord.Color.blue(),
        discord.Color.green(),
        discord.Color.purple(),
        discord.Color.orange(),
        discord.Color.teal(),
        discord.Color.magenta(),
    ]
    return palette[user_id % len(palette)]


class UpdateModal(discord.ui.Modal):
    def __init__(self, session_type):
        title = "Morning Update" if session_type == "morning" else "Evening Update"
        super().__init__(title=title, timeout=300)
        self.session_type = session_type

        if session_type == "morning":
            q1 = "What will you be working on today?"
            q2 = "Any challenges or support needed to get started?"
        else:
            q1 = "What did you achieve today?"
            q2 = "Anything pending or blocked?"

        self.answer1 = discord.ui.TextInput(label=q1, required=True, max_length=1000)
        self.answer2 = discord.ui.TextInput(label=q2, required=True, max_length=1000)
        self.add_item(self.answer1)
        self.add_item(self.answer2)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        state = session_state[self.session_type]

        if not state["active"]:
            await interaction.response.send_message("Session is not active right now.", ephemeral=True)
            return

        if user.id in state["replied_users"]:
            await interaction.response.send_message("You already submitted this update.", ephemeral=True)
            return

        today_str = get_today_str()
        now_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
        part1 = str(self.answer1.value).strip()
        part2 = str(self.answer2.value).strip()

        save_update(today_str, user, self.session_type, part1, part2, now_str)
        state["replied_users"].add(user.id)

        thread_id = state["thread_id"]
        thread = bot.get_channel(thread_id) if thread_id else None

        if self.session_type == "morning":
            q1 = "What will you be working on today?"
            q2 = "Any challenges or support needed to get started?"
            title = "Morning Update"
        else:
            q1 = "What did you achieve today?"
            q2 = "Anything pending or blocked?"
            title = "Evening Update"

        if thread is not None:
            embed = discord.Embed(
                title=f"{user.display_name} posted an update for {title}",
                color=get_user_color(user.id)
            )
            embed.add_field(name=q1, value=part1 or "-", inline=False)
            embed.add_field(name=q2, value=part2 or "-", inline=False)
            await thread.send(embed=embed)

        if interaction.guild is not None:
            main_channel = bot.get_channel(CHANNEL_ID)
            if main_channel is not None:
                await update_status_message(main_channel, interaction.guild, self.session_type)

        await interaction.response.send_message("Update submitted successfully.", ephemeral=True)


class StartUpdateView(discord.ui.View):
    def __init__(self, session_type):
        super().__init__(timeout=None)
        self.session_type = session_type
        self.start_update.custom_id = f"start_update_btn_{session_type}"

    @discord.ui.button(label="Start Update", style=discord.ButtonStyle.primary)
    async def start_update(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            user = interaction.user
            state = session_state[self.session_type]

            if not state["active"]:
                await interaction.response.send_message("Session is not active right now.", ephemeral=True)
                return

            if user.id in state["replied_users"]:
                await interaction.response.send_message("You already submitted this update.", ephemeral=True)
                return

            if user.id in state["user_steps"]:
                await interaction.response.send_message("Your update flow is already in progress.", ephemeral=True)
                return

            thread_id = state["thread_id"]
            thread = bot.get_channel(thread_id) if thread_id else None
            if thread is None:
                await interaction.response.send_message("Update thread not found. Please try again.", ephemeral=True)
                return

            await interaction.response.send_modal(UpdateModal(self.session_type))
        except Exception as e:
            print(f"[MESSAGE] Start Update button failed: {e!r}")
            if not interaction.response.is_done():
                await interaction.response.send_message("Failed to start update. Please try again.", ephemeral=True)

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
        session_text = "🌞 **Morning Update Time**\nClick **Start Update** and continue in the thread."
    else:
        session_text = "🌙 **Evening Update Time**\nClick **Start Update** and continue in the thread."

    session_message = await channel.send(session_text, view=StartUpdateView(session_type))
    print(f"[SESSION] Session message sent for {session_type}.")
    thread_name = f"{session_type}-updates-{get_today_str()}"
    session_thread = await session_message.create_thread(name=thread_name)
    print(f"[SESSION] Session thread created: thread_id={session_thread.id}")
    await session_thread.send(
        f"Use this thread for {session_type} updates.\n"
        "Click Start Update on the main session message."
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
    global views_registered
    print(f"[READY] Logged in as {bot.user}")
    print(f"[TIME] Configured timezone={TZ}, current_local_time={get_now().isoformat()}")
    print("[READY] Calling ensure_sheet_headers()")
    try:
        ensure_sheet_headers()
        print("[READY] ensure_sheet_headers() completed successfully")
    except Exception as e:
        print(f"[SHEET] ensure_sheet_headers() failed with exception: {e!r}")

    if not views_registered:
        bot.add_view(StartUpdateView("morning"))
        bot.add_view(StartUpdateView("evening"))
        views_registered = True
        print("[READY] Persistent button views registered")

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
    print(
        f"[MESSAGE] Ignored message content while session {active_session} is active; "
        "use Start Update button."
    )

bot.run(TOKEN)