import json
import os
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_session import Session
from threading import Thread
import asyncio
from datetime import datetime, timedelta
import pytz
from functools import wraps

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# Render dashboard URL (no more Replit redirect)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://bible-study-bot-14vt.onrender.com")

SCHEDULE_FILE = "schedule.json"
ACTIVE_MESSAGES_FILE = "active_messages.json"
DM_LOG_FILE = "dm_log.json"
CHAT_HISTORY_FILE = "chat_history.json"
ALLOWED_GUILD_ID = 1322203707768569856  # Lock bot to this server
REMINDER_CHANNEL_ID = 1443856322817953855  # Channel for 6-hour reminder pings
START_DATE = datetime(2025, 11, 29)  # Saturday, November 29, 2025
BRISBANE_TZ = pytz.timezone('Australia/Brisbane')
STUDY_HOUR = 19  # 7 PM
STUDY_MINUTE = 30

def log_dm(user_id, user_name, message_type, status="sent"):
    """Log DM sent to a user."""
    try:
        logs = []
        if os.path.exists(DM_LOG_FILE):
            with open(DM_LOG_FILE, "r") as f:
                logs = json.load(f)
        
        logs.append({
            "timestamp": datetime.now(BRISBANE_TZ).isoformat(),
            "user_id": user_id,
            "user_name": user_name,
            "type": message_type,
            "status": status
        })
        
        # Keep only last 100 entries
        if len(logs) > 100:
            logs = logs[-100:]
        
        with open(DM_LOG_FILE, "w") as f:
            json.dump(logs, f, indent=4)
    except Exception as e:
        print(f"Error logging DM: {e}")

def load_chat_history():
    """Load chat history."""
    if os.path.exists(CHAT_HISTORY_FILE):
        with open(CHAT_HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_chat_message(from_user, text):
    """Save a message to chat history, organized by user."""
    try:
        messages = load_chat_history()
        
        messages.append({
            "from": from_user,
            "text": text,
            "timestamp": datetime.now(BRISBANE_TZ).isoformat()
        })
        
        # Keep only last 100 messages
        if len(messages) > 100:
            messages = messages[-100:]
        
        with open(CHAT_HISTORY_FILE, "w") as f:
            json.dump(messages, f, indent=4)
    except Exception as e:
        print(f"Error saving chat message: {e}")

def get_dm_conversations():
    """Get list of unique DM conversations with last message."""
    try:
        messages = load_chat_history()
        conversations = {}
        
        for msg in messages:
            from_user = msg["from"]
            if from_user.startswith("user_"):
                user_id = from_user.replace("user_", "")
                username = msg.get("username", "Unknown")
                if user_id not in conversations:
                    conversations[user_id] = {
                        "username": username,
                        "last_message": msg["text"],
                        "last_timestamp": msg["timestamp"],
                        "user_from": from_user
                    }
                else:
                    conversations[user_id]["last_message"] = msg["text"]
                    conversations[user_id]["last_timestamp"] = msg["timestamp"]
                    conversations[user_id]["username"] = username
        
        return conversations
    except Exception as e:
        print(f"Error getting conversations: {e}")
        return {}

def get_user_messages(user_id):
    """Get all messages with a specific user."""
    try:
        messages = load_chat_history()
        user_from = f"user_{user_id}"
        
        user_messages = [msg for msg in messages if msg["from"] == user_from or msg["from"] == "admin"]
        return user_messages
    except Exception as e:
        print(f"Error getting user messages: {e}")
        return []

async def fetch_dm_history_from_discord(user_id):
    """Fetch DM history from Discord and merge with existing messages."""
    try:
        user_id_int = int(user_id)
        
        # Try to get the user
        user = bot.get_user(user_id_int)
        if not user:
            user = await bot.fetch_user(user_id_int)
        
        if not user:
            return False
        
        # Get DM channel with the user
        dm_channel = user.dm_channel
        if not dm_channel:
            dm_channel = await user.create_dm()
        
        # Fetch message history (get last 50 messages)
        messages = []
        async for msg in dm_channel.history(limit=50):
            # Skip bot's own messages (we already have those as "admin")
            # Reverse to get chronological order
            messages.insert(0, msg)
        
        # Load existing chat history
        existing_messages = load_chat_history()
        existing_user_messages = {msg["timestamp"] for msg in existing_messages if msg.get("from") == f"user_{user_id}"}
        
        # Add new messages to chat history
        for msg in messages:
            timestamp = msg.created_at.isoformat()
            
            # Skip if already exists
            if timestamp in existing_user_messages:
                continue
            
            # Skip bot's own messages (they're stored as "admin")
            if msg.author == bot.user:
                continue
            
            # Add user message
            existing_messages.append({
                "from": f"user_{user_id}",
                "username": msg.author.name,
                "user_id": str(user_id),
                "text": msg.content,
                "timestamp": timestamp
            })
            existing_user_messages.add(timestamp)
        
        # Keep only last 100 messages total
        if len(existing_messages) > 100:
            existing_messages = existing_messages[-100:]
        
        # Save updated history
        with open(CHAT_HISTORY_FILE, "w") as f:
            json.dump(existing_messages, f, indent=4)
        
        return True
    except Exception as e:
        print(f"Error fetching DM history: {e}")
        return False

def get_date_for_week(week_index):
    """Calculate the Saturday date for a given week index (0-based)."""
    return START_DATE + timedelta(weeks=week_index)

def format_date(date):
    """Format date as 'Sat 29/11'."""
    return date.strftime("%a %d/%m").replace("Sat", "Sat").replace("Sun", "Sun").replace("Mon", "Mon").replace("Tue", "Tue").replace("Wed", "Wed").replace("Thu", "Thu").replace("Fri", "Fri")

def parse_date_string(date_str):
    """Parse 'Sat 29/11' into the next matching Brisbane datetime."""
    try:
        day, month = date_str.split()[-1].split('/')
        now = datetime.now(BRISBANE_TZ)
        year = now.year

        target = BRISBANE_TZ.localize(datetime(year, int(month), int(day), STUDY_HOUR, STUDY_MINUTE, 0))

        # If the date has already passed this year, roll to next year
        if target < now:
            target = BRISBANE_TZ.localize(datetime(year + 1, int(month), int(day), STUDY_HOUR, STUDY_MINUTE, 0))

        return target
    except Exception:
        return None

def has_past_study_time(now):
    """Return True when the study time for the given day has already passed."""
    return now.hour > STUDY_HOUR or (now.hour == STUDY_HOUR and now.minute >= STUDY_MINUTE)

def get_next_study_time():
    """Get the next Bible study time (7:30 PM Brisbane time)."""
    now = datetime.now(BRISBANE_TZ)
    
    # Try to get date from first schedule entry
    current_schedule = load_schedule()
    if current_schedule:
        first_entry = current_schedule[0]
        if isinstance(first_entry, dict) and "date" in first_entry:
            scheduled_date = parse_date_string(first_entry["date"])
            if scheduled_date:
                if scheduled_date > now:
                    return scheduled_date

    # Fallback: Find the next Saturday
    days_until_saturday = (5 - now.weekday()) % 7
    if days_until_saturday == 0 and has_past_study_time(now):
        days_until_saturday = 7
    
    next_study = now + timedelta(days=days_until_saturday)
    next_study = next_study.replace(hour=STUDY_HOUR, minute=STUDY_MINUTE, second=0, microsecond=0)

    return next_study


def get_next_schedule_date(schedule):
    """Return the next available Saturday at the study time for a new schedule entry."""
    now = datetime.now(BRISBANE_TZ)

    if schedule:
        last_entry = schedule[-1]
        last_date = parse_date_string(last_entry.get("date", "")) if isinstance(last_entry, dict) else None
        if last_date:
            return last_date + timedelta(days=7)

    days_until_saturday = (5 - now.weekday()) % 7
    if days_until_saturday == 0 and has_past_study_time(now):
        days_until_saturday = 7

    next_saturday = now + timedelta(days=days_until_saturday)
    return next_saturday.replace(hour=STUDY_HOUR, minute=STUDY_MINUTE, second=0, microsecond=0)

def get_countdown():
    """Get countdown in seconds to next study time."""
    now = datetime.now(BRISBANE_TZ)
    next_study = get_next_study_time()
    delta = next_study - now
    return max(0, int(delta.total_seconds()))

def load_active_messages():
    try:
        with open(ACTIVE_MESSAGES_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_active_messages(messages):
    with open(ACTIVE_MESSAGES_FILE, "w") as f:
        json.dump(messages, f, indent=4)

def load_all_schedules():
    try:
        with open(SCHEDULE_FILE, "r") as f:
            data = json.load(f)
            return data.get("schedules", {})
    except (FileNotFoundError, json.JSONDecodeError):
        with open(SCHEDULE_FILE, "w") as f:
            json.dump({"schedules": {}}, f)
        return {}

def load_schedule(guild_id=None):
    """Load schedule for a specific guild or default."""
    guild_key = str(guild_id) if guild_id else "default"
    all_schedules = load_all_schedules()
    return all_schedules.get(guild_key, [])

def save_schedule(schedule_list, guild_id=None):
    """Save schedule for a specific guild or default."""
    guild_key = str(guild_id) if guild_id else "default"
    all_schedules = load_all_schedules()
    all_schedules[guild_key] = schedule_list
    with open(SCHEDULE_FILE, "w") as f:
        json.dump({"schedules": all_schedules}, f, indent=4)

def get_user_ids(schedule_list):
    return [entry["id"] if isinstance(entry, dict) else entry for entry in schedule_list]

def find_user_index(schedule_list, user_id):
    for i, entry in enumerate(schedule_list):
        entry_id = entry["id"] if isinstance(entry, dict) else entry
        if entry_id == user_id:
            return i
    return -1

intents = discord.Intents.default()
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def advance_schedule_if_needed():
    """Rotate the schedule once the current session time has passed."""
    schedule = load_schedule(None)
    if not schedule:
        return

    changed = False
    now = datetime.now(BRISBANE_TZ)

    while schedule:
        first_entry = schedule[0]
        if isinstance(first_entry, dict):
            first_date_str = first_entry.get("date", format_date(get_date_for_week(0)))
        else:
            first_date_str = format_date(get_date_for_week(0))

        scheduled_date = parse_date_string(first_date_str)
        if not scheduled_date:
            break

        if now >= scheduled_date:
            completed = schedule.pop(0)

            # Determine the next available date slot (one week after the last scheduled date)
            if schedule:
                last_entry = schedule[-1]
                last_date_str = last_entry.get("date", first_date_str) if isinstance(last_entry, dict) else first_date_str
                last_date = parse_date_string(last_date_str) or scheduled_date
            else:
                last_date = scheduled_date

            next_slot_date = last_date + timedelta(days=7)
            next_slot_str = format_date(next_slot_date)

            if isinstance(completed, dict):
                completed["date"] = next_slot_str
            else:
                completed = {"id": completed, "name": None, "date": next_slot_str}

            schedule.append(completed)
            changed = True
        else:
            break

    if changed:
        save_schedule(schedule, None)
        await update_all_schedule_messages()

async def refresh_member_names(guild: discord.Guild):
    """Refresh all member names in the schedule from Discord."""
    if not guild:
        return
    
    schedule = load_schedule(None)
    updated = False
    
    for i, entry in enumerate(schedule):
        if isinstance(entry, dict):
            user_id = entry["id"]
            stored_name = entry.get("name", "")
            
            # Fetch fresh member data from Discord
            member = guild.get_member(user_id)
            if member:
                current_name = member.display_name
                if stored_name != current_name:
                    entry["name"] = current_name
                    updated = True
    
    if updated:
        save_schedule(schedule, None)

async def format_schedule(guild: discord.Guild, guild_id=None):
    """Format schedule for display."""
    # Refresh all member names from Discord first
    await refresh_member_names(guild)
    
    # Always use "default" schedule (guild_id=None means use default key)
    schedule = load_schedule(None)
    text = ""
    updated = False
    
    # Add upcoming week header if schedule exists
    if schedule:
        first_entry = schedule[0]
        if isinstance(first_entry, dict):
            upcoming_name = first_entry["name"]
        else:
            upcoming_name = None
        
        if upcoming_name:
            text += f"# Upcoming Week: {upcoming_name}\n\n"
    
    text += "**Bible Study Leader Schedule:**\n"
    
    for i, entry in enumerate(schedule):
        if isinstance(entry, dict):
            user_id = entry["id"]
            stored_name = entry["name"]
            date_str = entry.get("date", format_date(get_date_for_week(i)))
        else:
            user_id = entry
            stored_name = None
            date_str = format_date(get_date_for_week(i))
        
        member = guild.get_member(user_id) if guild else None
        if member:
            current_name = member.display_name
            if not isinstance(entry, dict) or entry.get("name") != current_name:
                schedule[i] = {"id": user_id, "name": current_name, "date": date_str}
                updated = True
            name = current_name
        else:
            name = stored_name if stored_name else f"(Unknown) {user_id}"
        
        text += f"**{date_str}:** {name}\n"
    
    if updated:
        save_schedule(schedule, guild_id)
    
    return text

class ScheduleView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild
        if DASHBOARD_URL:
            self.add_item(discord.ui.Button(
                label="Web Dashboard",
                style=discord.ButtonStyle.link,
                url=DASHBOARD_URL,
                emoji="🌐"
            ))

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.danger, custom_id="pass_button")
    async def pass_week(self, interaction: discord.Interaction, button: discord.ui.Button):
        schedule = load_schedule(None)
        user_id = interaction.user.id
        user_ids = get_user_ids(schedule)

        if user_id not in user_ids:
            return await interaction.response.send_message(
                "You are not in the schedule.", ephemeral=True
            )

        first_id = schedule[0]["id"] if isinstance(schedule[0], dict) else schedule[0]
        if first_id != user_id:
            first_entry = schedule[0]
            if isinstance(first_entry, dict):
                first_date = first_entry.get("date", format_date(get_date_for_week(0)))
            else:
                first_date = format_date(get_date_for_week(0))
            return await interaction.response.send_message(
                f"Only the leader for **{first_date}** can pass.", ephemeral=True
            )

        skipped = schedule.pop(0)
        if isinstance(skipped, dict):
            skipped["name"] = interaction.user.display_name
        else:
            skipped = {"id": skipped, "name": interaction.user.display_name}
        schedule.append(skipped)
        
        # Recalculate dates based on position order (keep chronological)
        for i, entry in enumerate(schedule):
            if isinstance(entry, dict):
                entry["date"] = format_date(get_date_for_week(i))
        
        save_schedule(schedule, None)

        updated = await format_schedule(self.guild, None)
        next_leader = schedule[0]
        if isinstance(next_leader, dict):
            next_date = next_leader.get("date", format_date(get_date_for_week(0)))
        else:
            next_date = format_date(get_date_for_week(0))

        await interaction.response.edit_message(
            content=f"**They passed! Next leader for {next_date}:**\n\n" + updated,
            view=ScheduleView(self.guild)
        )
        
        await update_all_schedule_messages()

active_messages = load_active_messages()

async def update_all_schedule_messages():
    global active_messages
    messages_to_remove = []
    
    for msg_info in active_messages:
        try:
            guild = bot.get_guild(msg_info["guild_id"])
            if not guild:
                messages_to_remove.append(msg_info)
                continue
                
            channel = guild.get_channel(msg_info["channel_id"])
            if not channel:
                messages_to_remove.append(msg_info)
                continue
            
            try:
                message = await channel.fetch_message(msg_info["message_id"])
                text = await format_schedule(guild, None)
                await message.edit(content=text, view=ScheduleView(guild))
            except discord.NotFound:
                messages_to_remove.append(msg_info)
            except discord.Forbidden:
                messages_to_remove.append(msg_info)
        except Exception:
            pass
    
    for msg_info in messages_to_remove:
        if msg_info in active_messages:
            active_messages.remove(msg_info)
    
    save_active_messages(active_messages)

@bot.tree.command(name="schedule", description="Show the Bible study schedule.")
async def show_schedule(interaction: discord.Interaction):
    if interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("❌ This bot only works in the designated server.", ephemeral=True)
    
    global active_messages
    text = await format_schedule(interaction.guild, None)
    await interaction.response.send_message(
        content=text,
        view=ScheduleView(interaction.guild)
    )
    
    message = await interaction.original_response()
    msg_info = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "message_id": message.id
    }
    
    active_messages = [m for m in active_messages if m["message_id"] != message.id]
    active_messages.append(msg_info)
    save_active_messages(active_messages)

@bot.tree.command(name="add", description="Add a leader to the schedule.")
async def add_user(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("❌ This bot only works in the designated server.", ephemeral=True)
    
    schedule = load_schedule(None)
    
    next_date = format_date(get_next_schedule_date(schedule))
    schedule.append({"id": user.id, "name": user.display_name, "date": next_date})
    save_schedule(schedule, None)
    text = await format_schedule(interaction.guild, None)
    await interaction.response.send_message(f"Added **{user.display_name}**.\n\n" + text)
    await update_all_schedule_messages()

@bot.tree.command(name="remove", description="Remove a leader from the schedule.")
async def remove_user(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("❌ This bot only works in the designated server.", ephemeral=True)
    
    schedule = load_schedule(None)
    index = find_user_index(schedule, user.id)
    
    if index == -1:
        return await interaction.response.send_message(
            "**User is not in the schedule.**",
            ephemeral=True
        )

    schedule.pop(index)
    save_schedule(schedule, None)
    text = await format_schedule(interaction.guild, None)
    await interaction.response.send_message(f"Removed **{user.display_name}**.\n\n" + text)
    await update_all_schedule_messages()

last_reminder_date = None
last_6h_reminder_time = None

async def send_24h_reminders():
    """Send DM reminders to leaders 24 hours before their session."""
    global last_reminder_date
    now = datetime.now(BRISBANE_TZ)

    next_study = get_next_study_time()

    # Prevent multiple sends for the same study date
    if last_reminder_date == next_study.date():
        return

    try:
        time_until = (next_study - now).total_seconds()

        if 23 * 3600 < time_until < 25 * 3600:
            for guild in bot.guilds:
                current_schedule = load_schedule(None)
                if current_schedule:
                    leader_entry = current_schedule[0]
                    leader_id = leader_entry["id"] if isinstance(leader_entry, dict) else leader_entry
                    leader_name = leader_entry.get("name", "Leader") if isinstance(leader_entry, dict) else "Leader"

                    member = guild.get_member(leader_id)
                    if member:
                        try:
                            # Use Discord timestamp (shows in user's local timezone)
                            study_timestamp = int(next_study.timestamp())
                            await member.send(f"📖 Reminder: You're leading Bible Study on <t:{study_timestamp}:F> (<t:{study_timestamp}:R>)!")
                            log_dm(leader_id, member.display_name, "24h_reminder", "sent")
                            last_reminder_date = next_study.date()
                        except Exception as e:
                            log_dm(leader_id, leader_name, "24h_reminder", "failed")
                            print(f"Could not DM {member}: {e}")
    except Exception as e:
        print(f"Error in send_24h_reminders: {e}")

async def send_6h_reminders():
    """Send channel ping reminders to leaders 6 hours before their session."""
    global last_6h_reminder_time
    now = datetime.now(BRISBANE_TZ)

    if last_6h_reminder_time and (now - last_6h_reminder_time).total_seconds() < 3600:
        return

    try:
        next_study = get_next_study_time()
        time_until = (next_study - now).total_seconds()

        if 5.5 * 3600 < time_until < 6.5 * 3600:
            for guild in bot.guilds:
                if guild.id == ALLOWED_GUILD_ID:
                    current_schedule = load_schedule(None)
                    if current_schedule:
                        leader_entry = current_schedule[0]
                        leader_id = leader_entry["id"] if isinstance(leader_entry, dict) else leader_entry
                        
                        channel = guild.get_channel(REMINDER_CHANNEL_ID)
                        if channel:
                            try:
                                study_timestamp = int(next_study.timestamp())
                                member = guild.get_member(leader_id)
                                member_name = member.display_name if member else "Unknown"
                                await channel.send(f"<@{leader_id}> - Your Bible Study session is <t:{study_timestamp}:R> 📖")
                                log_dm(leader_id, member_name, "6h_reminder", "sent")
                                last_6h_reminder_time = now
                            except Exception as e:
                                log_dm(leader_id, "Unknown", "6h_reminder", "failed")
                                print(f"Could not send 6h reminder: {e}")
    except Exception as e:
        print(f"Error in send_6h_reminders: {e}")

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Handle member updates (nickname changes, etc.)"""
    # Only update for allowed guild
    if after.guild.id != ALLOWED_GUILD_ID:
        return
    
    # Check if display name changed
    if before.display_name == after.display_name:
        return
    
    # Find user in schedule and update their name
    schedule = load_schedule(None)
    index = find_user_index(schedule, after.id)
    
    if index != -1:
        entry = schedule[index]
        if isinstance(entry, dict):
            entry["name"] = after.display_name
            save_schedule(schedule, None)
            
            # Update all Discord messages to reflect the name change
            await update_all_schedule_messages()

@bot.event
async def on_message(message):
    """Handle incoming DMs from users."""
    # Ignore bot's own messages
    if message.author == bot.user:
        return
    
    # Only process DMs (private channels)
    if isinstance(message.channel, discord.DMChannel):
        # Store the message with username
        try:
            messages = load_chat_history()
            messages.append({
                "from": f"user_{message.author.id}",
                "username": message.author.name,
                "user_id": str(message.author.id),
                "text": message.content,
                "timestamp": datetime.now(BRISBANE_TZ).isoformat()
            })
            
            # Keep only last 100 messages
            if len(messages) > 100:
                messages = messages[-100:]
            
            with open(CHAT_HISTORY_FILE, "w") as f:
                json.dump(messages, f, indent=4)
        except Exception as e:
            print(f"Error saving message: {e}")
        
        print(f"DM from {message.author}: {message.content}")
    
    # Process commands
    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()
    print("Slash commands synced.")
    bot.add_view(ScheduleView(None))
    
    async def reminder_loop():
        while True:
            await asyncio.sleep(60)
            await advance_schedule_if_needed()
            await send_24h_reminders()
            await send_6h_reminders()
    
    bot.loop.create_task(reminder_loop())

def trigger_discord_update():
    try:
        loop = bot.loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(update_all_schedule_messages(), loop)
    except Exception as e:
        print(f"Error triggering Discord update: {e}")

app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SECRET_KEY'] = 'bible-study-bot-secret'
Session(app)

# Health check endpoint for cron jobs / uptime monitors
@app.route('/health')
def health():
    return "ok", 200

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'admin'

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('home'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
def home():
    current_schedule = load_schedule(None)
    schedule_data = []
    for i, entry in enumerate(current_schedule):
        if isinstance(entry, dict):
            date_str = entry.get("date", format_date(get_date_for_week(i)))
            schedule_data.append({
                "id": entry["id"],
                "name": entry["name"],
                "date": date_str
            })
        else:
            date_str = format_date(get_date_for_week(i))
            schedule_data.append({
                "id": entry,
                "name": f"User {entry}",
                "date": date_str
            })
    
    bot_name = "Bot starting..." if not bot.user else str(bot.user)
    
    return render_template('dashboard.html', 
                           schedule=schedule_data,
                           bot_name=bot_name)

@app.route('/api/update-date', methods=['POST'])
def update_date():
    try:
        data = request.get_json()
        user_id = int(data.get('id'))
        new_date = data.get('date')
        
        schedule = load_schedule(None)
        for entry in schedule:
            if entry.get('id') == user_id:
                entry['date'] = new_date
                break
        
        save_schedule(schedule, None)
        asyncio.run_coroutine_threadsafe(update_all_schedule_messages(), bot.loop)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/reorder', methods=['POST'])
def reorder_schedule():
    try:
        data = request.get_json()
        new_order = data.get('schedule', [])
        
        converted_order = []
        for entry in new_order:
            converted_order.append({
                "id": int(entry["id"]) if isinstance(entry["id"], str) else entry["id"],
                "name": entry["name"],
                "date": entry.get("date", format_date(get_date_for_week(len(converted_order))))
            })
        
        save_schedule(converted_order, None)
        trigger_discord_update()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/members')
def get_members():
    try:
        members_list = []
        current_user_ids = get_user_ids(load_schedule(None))
        
        for guild in bot.guilds:
            for member in guild.members:
                if not member.bot and member.id not in current_user_ids:
                    members_list.append({
                        "id": str(member.id),
                        "name": member.display_name,
                        "username": str(member),
                        "status": str(getattr(member, "status", "offline")),
                        "avatar": str(member.display_avatar.url) if member.display_avatar else None
                    })
        
        members_list.sort(key=lambda x: x["name"].lower())
        return jsonify({"success": True, "members": members_list})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "members": []})


@app.route('/api/server-members')
def get_server_members():
    """Return all non-bot members with profile pictures and status."""
    try:
        members_list = []

        for guild in bot.guilds:
            for member in guild.members:
                if member.bot:
                    continue

                status = str(getattr(member, "status", "offline"))
                avatar_url = str(member.display_avatar.url) if member.display_avatar else None

                members_list.append({
                    "id": str(member.id),
                    "name": member.display_name,
                    "username": str(member),
                    "status": status,
                    "avatar": avatar_url
                })

        members_list.sort(key=lambda x: x["name"].lower())
        return jsonify({"success": True, "members": members_list})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "members": []})

@app.route('/api/add', methods=['POST'])
def api_add_user():
    try:
        data = request.get_json()
        user_id = int(data.get('id'))
        user_name = data.get('name')
        
        schedule = load_schedule(None)
        user_ids = get_user_ids(schedule)
        if user_id in user_ids:
            return jsonify({"success": False, "error": "User already in schedule"})
        
        schedule.append({"id": user_id, "name": user_name, "date": format_date(get_next_schedule_date(schedule))})
        save_schedule(schedule, None)
        trigger_discord_update()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/remove', methods=['POST'])
def api_remove_user():
    try:
        data = request.get_json()
        user_id = int(data.get('id'))
        
        schedule = load_schedule(None)
        index = find_user_index(schedule, user_id)
        if index == -1:
            return jsonify({"success": False, "error": "User not in schedule"})
        
        schedule.pop(index)
        save_schedule(schedule, None)
        trigger_discord_update()
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/countdown')
def get_countdown_api():
    try:
        countdown_seconds = get_countdown()
        hours = countdown_seconds // 3600
        minutes = (countdown_seconds % 3600) // 60
        seconds = countdown_seconds % 60
        return jsonify({
            "success": True,
            "seconds": countdown_seconds,
            "hours": hours,
            "minutes": minutes,
            "seconds_display": seconds,
            "formatted": f"{hours}h {minutes}m {seconds}s"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/login', methods=['POST'])
def api_login():
    """Login endpoint."""
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Invalid credentials"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/logout')
def api_logout():
    """Logout endpoint."""
    session.clear()
    return jsonify({"success": True})

@app.route('/api/check-login')
def check_login():
    """Check if user is logged in."""
    return jsonify({"logged_in": 'logged_in' in session})

@app.route('/api/test-24h-reminder', methods=['POST'])
def test_24h_reminder():
    """Test endpoint to trigger 24h DM reminder to current leader."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        def send_test_reminder():
            current_schedule = load_schedule(None)
            if not current_schedule:
                return False

            leader_entry = current_schedule[0]
            leader_id = leader_entry["id"] if isinstance(leader_entry, dict) else leader_entry

            for guild in bot.guilds:
                member = guild.get_member(leader_id)
                if member:
                    next_study = get_next_study_time()
                    study_timestamp = int(next_study.timestamp())
                    asyncio.run_coroutine_threadsafe(
                        member.send(f"📖 Test Reminder (24h): You're leading Bible Study on <t:{study_timestamp}:F> (<t:{study_timestamp}:R>)!"),
                        bot.loop
                    )
                    return True
            return False
        
        success = send_test_reminder()
        return jsonify({
            "success": success,
            "message": "24h test reminder sent!" if success else "No leader found or bot not ready"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/dm-log')
def get_dm_log():
    """Get DM log for admin view."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated", "logs": []})
    
    try:
        logs = []
        if os.path.exists(DM_LOG_FILE):
            with open(DM_LOG_FILE, "r") as f:
                logs = json.load(f)
        
        # Return in reverse order (most recent first)
        return jsonify({"success": True, "logs": logs[::-1]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "logs": []})

@app.route('/api/dm-conversations')
def get_conversations():
    """Get list of DM conversations."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "conversations": []})
    
    try:
        convs = get_dm_conversations()
        conversations = []
        for user_id, data in convs.items():
            conversations.append({
                "user_id": user_id,
                "username": data.get("username", "Unknown"),
                "last_message": data["last_message"][:50] + "..." if len(data["last_message"]) > 50 else data["last_message"],
                "last_timestamp": data["last_timestamp"]
            })
        
        # Sort by timestamp descending
        conversations.sort(key=lambda x: x["last_timestamp"], reverse=True)
        return jsonify({"success": True, "conversations": conversations})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "conversations": []})

@app.route('/api/user-messages/<user_id>')
def get_user_dm(user_id):
    """Get messages with a specific user."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "messages": []})
    
    try:
        messages = get_user_messages(user_id)
        return jsonify({"success": True, "messages": messages})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "messages": []})

@app.route('/api/chat-history')
def get_chat_history():
    """Get chat history for admin."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "messages": []})
    
    try:
        messages = load_chat_history()
        return jsonify({"success": True, "messages": messages})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "messages": []})

@app.route('/api/send-chat-message', methods=['POST'])
def send_chat_message():
    """Send a chat message as admin to a specific user."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        data = request.json
        text = data.get('text', '').strip()
        user_id = data.get('user_id', '').strip()
        
        if not text:
            return jsonify({"success": False, "error": "Message cannot be empty"})
        if not user_id:
            return jsonify({"success": False, "error": "User ID required"})
        
        # Save to chat history
        save_chat_message("admin", text)
        
        # Send actual Discord DM to the user
        def send_dm():
            try:
                user_id_int = int(user_id)
                
                # Try to find user in guild first
                for guild in bot.guilds:
                    member = guild.get_member(user_id_int)
                    if member:
                        asyncio.run_coroutine_threadsafe(
                            member.send(text),
                            bot.loop
                        )
                        return True
                
                # If not in guild, try to fetch user directly
                user = bot.get_user(user_id_int)
                if user:
                    asyncio.run_coroutine_threadsafe(
                        user.send(text),
                        bot.loop
                    )
                    return True
                else:
                    # Try to fetch the user (this is async but we'll handle it)
                    async def fetch_and_send():
                        try:
                            user = await bot.fetch_user(user_id_int)
                            await user.send(text)
                            return True
                        except Exception as e:
                            print(f"Error fetching/sending DM to {user_id}: {e}")
                            return False
                    
                    future = asyncio.run_coroutine_threadsafe(fetch_and_send(), bot.loop)
                    return future.result(timeout=5)
                    
            except Exception as e:
                print(f"Error sending DM to {user_id}: {e}")
            return False
        
        success = send_dm()
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Could not send DM - user not found or bot lacking permissions"})
    except Exception as e:
        print(f"Exception in send_chat_message: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/delete-user-conversation/<user_id>', methods=['DELETE'])
def delete_user_conversation(user_id):
    """Delete all messages from a specific user."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        messages = load_chat_history()
        user_from = f"user_{user_id}"
        
        # Remove all messages from this user
        messages = [msg for msg in messages if msg.get("from") != user_from]
        
        with open(CHAT_HISTORY_FILE, "w") as f:
            json.dump(messages, f, indent=4)
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/rename-user/<user_id>', methods=['POST'])
def rename_user(user_id):
    """Rename a user in the chat history."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        data = request.json
        new_name = data.get('new_name', '').strip()
        
        if not new_name:
            return jsonify({"success": False, "error": "Name cannot be empty"})
        
        messages = load_chat_history()
        user_from = f"user_{user_id}"
        
        # Update username for all messages from this user
        for msg in messages:
            if msg.get("from") == user_from:
                msg["username"] = new_name
        
        with open(CHAT_HISTORY_FILE, "w") as f:
            json.dump(messages, f, indent=4)
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/load-dm-history/<user_id>', methods=['POST'])
def load_dm_history(user_id):
    """Load previous DM history from Discord."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        future = asyncio.run_coroutine_threadsafe(
            fetch_dm_history_from_discord(user_id),
            bot.loop
        )
        success = future.result(timeout=10)
        
        if success:
            return jsonify({"success": True, "message": "DM history loaded from Discord!"})
        else:
            return jsonify({"success": False, "error": "Could not load history - user not found or no messages"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/add-bot-message', methods=['POST'])
def add_bot_message():
    """Add an incoming bot message."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        data = request.json
        text = data.get('text', '').strip()
        if not text:
            return jsonify({"success": False, "error": "Message cannot be empty"})
        
        save_chat_message("bot", text)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/test-6h-reminder', methods=['POST'])
def test_6h_reminder():
    """Test endpoint to trigger 6h channel ping reminder to current leader."""
    if 'logged_in' not in session:
        return jsonify({"success": False, "error": "Not authenticated"})
    
    try:
        def send_test_6h_reminder():
            current_schedule = load_schedule(None)
            if not current_schedule:
                return False
            
            leader_entry = current_schedule[0]
            leader_id = leader_entry["id"] if isinstance(leader_entry, dict) else leader_entry
            
            for guild in bot.guilds:
                if guild.id == ALLOWED_GUILD_ID:
                    channel = guild.get_channel(REMINDER_CHANNEL_ID)
                    if channel:
                        next_study = get_next_study_time()
                        study_timestamp = int(next_study.timestamp())
                        asyncio.run_coroutine_threadsafe(
                            channel.send(f"🧪 Test: <@{leader_id}> - Your Bible Study session is <t:{study_timestamp}:R> 📖"),
                            bot.loop
                        )
                        return True
            return False

        
        success = send_test_6h_reminder()
        return jsonify({
            "success": success,
            "message": "6h test reminder sent!" if success else "No channel found or bot not ready"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

def run():
    app.run(host='0.0.0.0', port=5000)


def start_services():
    Thread(target=run).start()

    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not found in environment variables!")
        print("Please add your Discord bot token as a secret.")
    else:
        bot.run(TOKEN)


if __name__ == "__main__":
    start_services()


