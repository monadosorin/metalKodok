import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
import re
import json
import os
import asyncpg
from openai import OpenAI
from collections import defaultdict
from datetime import datetime, timedelta
import asyncio
from collections import defaultdict
from discord import HTTPException
from apscheduler.triggers.cron import CronTrigger
import tempfile
import time
from aiohttp import web
import asyncio, asyncpg
import io
import wave
try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    from google.oauth2 import service_account as google_sa
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False
    print("[gemini tts] google-genai not installed. Add `google-genai` to requirements.txt")


active_tts_user = None
last_tts_activity = 0
tts_voice_client = None


conversation_histories = defaultdict(list)
MAX_HISTORY = 5 
SESSION_TIMEOUT = 180 

locks = defaultdict(asyncio.Lock)


MESSAGE_COOLDOWN = 1.5 
USER_COOLDOWN = 3.0 


message_queue = asyncio.Queue()
processing_lock = asyncio.Lock()



DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# ===== Gemini TTS (Vertex AI) =====
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCP_REGION = os.getenv("GCP_REGION", "us-central1")
TTS_VOICE = os.getenv("TTS_VOICE", "Kore")
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-2.5-flash-tts")
TTS_SAMPLE_RATE = 24000  # Gemini TTS LINEAR16 output rate

gemini_tts_client = None
if _GENAI_AVAILABLE and GCP_PROJECT_ID:
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            credentials = google_sa.Credentials.from_service_account_info(
                json.loads(creds_json),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            # Fall back to GOOGLE_APPLICATION_CREDENTIALS env var (file path)
            credentials = None
        gemini_tts_client = google_genai.Client(
            vertexai=True,
            project=GCP_PROJECT_ID,
            location=GCP_REGION,
            credentials=credentials,
        )
        print(f"[gemini tts] client initialized (project={GCP_PROJECT_ID}, region={GCP_REGION})")
    except Exception as e:
        print(f"[gemini tts] failed to init client: {e}")
        gemini_tts_client = None


def _pcm_to_wav_bytes(pcm_bytes, sample_rate=TTS_SAMPLE_RATE):
    """Wrap raw 16-bit mono PCM in a WAV container so FFmpeg can play it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


TTS_STYLE_PREFIX = (
    "You are a TTS engine. Repeat aloud, verbatim, with natural pronunciation, "
    "the literal string given. Do NOT complete partial words. Do NOT translate. "
    "Do NOT respond. Do NOT add commentary. Even fragments, single characters, "
    "or gibberish must be spoken exactly as written."
)


async def synthesize_tts_audio(text):
    """Generate TTS audio (WAV bytes) using Gemini TTS, or None on failure."""
    if not gemini_tts_client:
        return None

    # Wrap text in triple-quote delimiters so the model treats it as a literal string.
    safe_text = text.replace('"""', '“””')
    prompt = (
        f"{TTS_STYLE_PREFIX}\n\n"
        f"String to speak (read EXACTLY this, no completion, no response):\n"
        f'"""\n{safe_text}\n"""'
    )

    def _call():
        return gemini_tts_client.models.generate_content(
            model=TTS_MODEL,
            contents=prompt,
            config=google_genai_types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=google_genai_types.SpeechConfig(
                    voice_config=google_genai_types.VoiceConfig(
                        prebuilt_voice_config=google_genai_types.PrebuiltVoiceConfig(
                            voice_name=TTS_VOICE,
                        )
                    )
                ),
            ),
        )

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _call)
        try:
            pcm_bytes = response.candidates[0].content.parts[0].inline_data.data
        except (AttributeError, IndexError, TypeError) as e:
            print(f"[gemini tts] unexpected response shape: {e}; raw response: {response}")
            return None
        return _pcm_to_wav_bytes(pcm_bytes)
    except Exception as e:
        print(f"[gemini tts] synth error: {e}")
        return None




# Unicode emoji ranges — broad enough to catch standard emojis without false-positives on regular text.
UNICODE_EMOJI_RE = re.compile(
    "("
    "[\U0001F1E0-\U0001F1FF]"    # flags
    "|[\U0001F300-\U0001F5FF]"   # symbols & pictographs
    "|[\U0001F600-\U0001F64F]"   # emoticons (smiley/sad/etc)
    "|[\U0001F680-\U0001F6FF]"   # transport & map
    "|[\U0001F700-\U0001F77F]"   # alchemical
    "|[\U0001F780-\U0001F7FF]"   # geometric shapes ext
    "|[\U0001F800-\U0001F8FF]"   # supplemental arrows
    "|[\U0001F900-\U0001F9FF]"   # supplemental symbols & pictographs
    "|[\U0001FA00-\U0001FA6F]"   # chess
    "|[\U0001FA70-\U0001FAFF]"   # symbols ext-a
    "|[\U00002600-\U000026FF]"   # misc symbols (incl ☀ ⚡ etc)
    "|[\U00002700-\U000027BF]"   # dingbats
    "|[\U0001F3FB-\U0001F3FF]"   # skin tone modifiers
    "|\U0000200D"                  # zero-width joiner
    "|\U0000FE0F"                  # variation selector
    ")+",
    re.UNICODE,
)


def clean_for_tts(message):
    """Strip Discord-specific tokens (emojis, mentions, markdown, URLs) so the TTS reads naturally."""
    text = message.content

    # Custom emojis: <:name:id> or <a:name:id>  ->  name
    text = re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r"\1", text)

    # User mentions: <@123> or <@!123>  ->  display name
    def _user_repl(m):
        uid = int(m.group(1))
        member = message.guild.get_member(uid) if message.guild else None
        return member.display_name if member else ""
    text = re.sub(r"<@!?(\d+)>", _user_repl, text)

    # Channel mentions: <#123>  ->  #channel-name
    def _ch_repl(m):
        cid = int(m.group(1))
        channel = bot.get_channel(cid)
        return f"#{channel.name}" if channel else ""
    text = re.sub(r"<#(\d+)>", _ch_repl, text)

    # Role mentions: <@&123>  ->  @role-name
    def _role_repl(m):
        rid = int(m.group(1))
        role = message.guild.get_role(rid) if message.guild else None
        return f"@{role.name}" if role else ""
    text = re.sub(r"<@&(\d+)>", _role_repl, text)

    # URLs  ->  "link"
    text = re.sub(r"https?://\S+", "link", text)

    # Strip Unicode emojis — Gemini TTS interprets them as vocal/emotional cues
    # and ends up making weird sounds (crying, moaning, etc.) instead of just reading text.
    text = UNICODE_EMOJI_RE.sub("", text)

    # Markdown formatting: strip the markers, keep the text
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)            # ***bold italic***
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                  # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)                          # __underline__
    text = re.sub(r"\*([^*\s].*?)\*", r"\1", text)                 # *italic*
    text = re.sub(r"~~(.+?)~~", r"\1", text)                          # ~~strike~~
    text = re.sub(r"\|\|(.+?)\|\|", r"\1", text)                  # ||spoiler||
    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.+?)\n?```", r"\1", text, flags=re.DOTALL)  # ```code```
    text = re.sub(r"`(.+?)`", r"\1", text)                            # `inline code`
    text = re.sub(r"^>+\s+", "", text, flags=re.MULTILINE)            # > blockquote

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text

# ===== TTS message queue (so rapid messages don't get dropped) =====
TTS_QUEUE_MAX = 50
tts_message_queue = asyncio.Queue(maxsize=TTS_QUEUE_MAX)
_tts_worker_started = False


async def tts_worker():
    """Pulls messages off the queue and TTSes them one at a time, in order."""
    while True:
        message = await tts_message_queue.get()
        try:
            if active_tts_user != message.author.id:
                continue
            if not (tts_voice_client and tts_voice_client.is_connected()):
                continue

            cleaned = clean_for_tts(message)
            if not cleaned:
                print("[tts] cleaned message is empty (probably just emojis/mentions), skipping")
                continue

            wav_bytes = await synthesize_tts_audio(cleaned)
            if not wav_bytes:
                continue

            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                temp_path = f.name
                f.write(wav_bytes)

            if not (tts_voice_client and tts_voice_client.is_connected()):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                continue

            while tts_voice_client and tts_voice_client.is_playing():
                await asyncio.sleep(0.05)

            audio_source = discord.FFmpegPCMAudio(temp_path)
            tts_voice_client.play(
                audio_source,
                after=lambda e: cleanup_tts_file_sync(temp_path, e),
            )
            print(f"[tts] playing queued message ({tts_message_queue.qsize()} left in queue)")

            while tts_voice_client and tts_voice_client.is_playing():
                await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[tts] worker error: {e}")
        finally:
            tts_message_queue.task_done()


def ensure_tts_worker_started():
    """Idempotent: starts the worker once per bot lifetime."""
    global _tts_worker_started
    if _tts_worker_started:
        return
    _tts_worker_started = True
    asyncio.create_task(tts_worker())
    print("[tts] worker task started")


BOT_NAME = "Metal Kodok"
PERSONALITY = """
You’re sassy, witty, and enjoy a dry sense of humor with a touch of sarcasm. You drop an occasional Indonesian swear word, but only when it fits the mood. Your jokes are lighthearted and fun, keeping things playful without going overboard.

You’re confident but not overbearing, and you know how to keep the vibe casual. You're not afraid to throw in a little playful jab now and then, but you don't overdo it. Teasing is subtle, and you know when to pull back. 

Keep your responses short, to the point, and engaging. You balance humor with subtlety, making sure everyone in the conversation feels included without focusing too much on any one person.
"""

async def handle_ping(request):
    print("[PING] External ping received, waking DB...")

    for attempt in range(5):  # try 5 times
        try:
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.execute("SELECT 1;")
            await conn.close()
            print(f"[PING] DB wake successful on attempt {attempt+1}")
            return web.Response(text="pong")
        except Exception as e:
            print(f"[PING] Attempt {attempt+1} failed: {e}")
            await asyncio.sleep(3)  # wait 3 seconds before retry

    print("[PING] Failed to wake DB after 5 attempts")
    return web.Response(text="error: database still asleep", status=500)
async def start_ping_server():
    app = web.Application()
    app.router.add_get("/ping", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)  # Railway defaults to 8080
    await site.start()
    print("[PING] Ping server started on port 8080")

async def get_history_key(message):
    """Create a unique key for conversation tracking (user + channel)"""
    return (message.author.id, message.channel.id)


async def add_to_history(key, role, content):
    """Add message to conversation history"""
    conversation_histories[key].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()  # Store as ISO string instead of datetime object
    })
    if len(conversation_histories[key]) > MAX_HISTORY:
        conversation_histories[key] = conversation_histories[key][-MAX_HISTORY:]



async def clear_expired_sessions():
    """Clean up old conversations more efficiently"""
    now = datetime.now()
    expired_keys = []

    for key, history in conversation_histories.items():
        if history:
            last_timestamp = datetime.fromisoformat(history[-1].get('timestamp', now.isoformat()))
            if (now - last_timestamp).total_seconds() > SESSION_TIMEOUT:
                expired_keys.append(key)

  
    for i in range(0, len(expired_keys), 100):
        batch = expired_keys[i:i + 100]
        for key in batch:
            del conversation_histories[key]
        await asyncio.sleep(0.5) 


async def ask_deepseek(history_key, retry_count=3):
    """Query DeepSeek with conversation history and retry logic"""
    for attempt in range(retry_count):
        try:
            messages = [{"role": "system", "content": PERSONALITY}]
            messages += [{"role": msg["role"], "content": msg["content"]}
                         for msg in conversation_histories.get(history_key, [])]

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                stream=False
            )
            return {
                "content": response.choices[0].message.content.strip(),
                "error": False
            }
        except Exception as e:
            if attempt < retry_count - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
            else:
                return {
                    "content": f"Sorry bro im tweaking, error nih: {str(e)}.",
                    "error": True
                }



async def safe_reply(message, response):
    """Handle message replies with rate limit protection"""
    try:
        await message.reply(f"{response}", mention_author=False)
    except HTTPException as e:
        if e.status == 429:
            retry_after = e.retry_after
            print(f"Rate limited. Retrying after {retry_after} seconds.")
            await asyncio.sleep(retry_after)
            await safe_reply(message, response)
        else:
            raise

async def message_processor():
    while True:
        message, response = await message_queue.get()
        async with processing_lock:
            try:
                await message.reply(response, mention_author=False)
            except HTTPException as e:
                if e.status == 429:
                    print(f"Rate limited. Retrying after {e.retry_after}s")
                    await asyncio.sleep(e.retry_after)
                    await message_queue.put((message, response))
            except Exception as e:
                print(f"Failed to send message: {str(e)}")
            finally:
                await asyncio.sleep(MESSAGE_COOLDOWN)
                message_queue.task_done()


DATABASE_URL = os.getenv("DATABASE_URL")



async def init_db():
    """Initialize the database connection with connection pooling"""
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=60
        )
        # Test the connection
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            # Ensure hangouts table exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS hangouts (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT,
                    creator_id BIGINT NOT NULL,
                    event_date DATE NOT NULL,
                    location TEXT,
                    description TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW(),
                    reminded BOOLEAN DEFAULT FALSE
                )
            """)
            # Migration: add event_time column if missing
            await conn.execute("ALTER TABLE hangouts ADD COLUMN IF NOT EXISTS event_time TIME")
            # Ensure swear_counts table exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS swear_counts (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        return pool
    except Exception as e:
        print(f"Error connecting to the database: {e}")
        return None

db_pool = None

QOTD_CHANNEL_ID = 1306689528211308575

intents = discord.Intents.default()
intents.message_content = True
intents.members = True 
intents.presences = True 
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler()



async def get_qotd():
    if not await ensure_db_pool():
        # return None and let caller send a fallback message or retry later
        return None
    try:
        async with db_pool.acquire() as conn:
            question = await conn.fetchrow("SELECT id, question FROM questions LIMIT 1")
            if question:
                await conn.execute("INSERT INTO used_questions (question_id) VALUES ($1)", question["id"])
                await conn.execute("DELETE FROM questions WHERE id = $1", question["id"])
                return question["question"]
            return None
    except Exception as e:
        print(f"get_qotd DB error: {e}")
        # attempt reconnect once
        await reconnect_database()
        return None

async def add_coordinate(name, x, z):
    """Add a coordinate to the database."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO coordinates (name, x, z) VALUES ($1, $2, $3)",
            name, x, z
        )


async def delete_coordinate(name):
    """Delete a coordinate from the database."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM coordinates WHERE name = $1", name)


async def list_coordinates():
    """Retrieve all coordinates from the database."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT name, x, z FROM coordinates")
        return [{"name": row["name"], "x": row["x"], "z": row["z"]} for row in rows]

# place near your db helpers
async def ensure_db_pool(retries=6, base_delay=1):
    global db_pool
    for attempt in range(retries):
        try:
            if db_pool:
                # quick health check
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute("SELECT 1")
                        return True
                except Exception:
                    # broken pool — close and reinit
                    try:
                        await db_pool.close()
                    except Exception:
                        pass
                    db_pool = None

            db_pool = await init_db()
            if db_pool:
                return True
        except Exception as e:
            print(f"DB ensure attempt {attempt+1} failed: {e}")
        await asyncio.sleep(base_delay * (2 ** attempt))  # exponential backoff
    return False

async def send_qotd():
    try:
        if not await ensure_db_pool():
            print("DB not available for QOTD")
            return
        question = await get_qotd()
        channel = bot.get_channel(QOTD_CHANNEL_ID)
        if not channel:
            print(f"❌ Could not find channel with ID {QOTD_CHANNEL_ID}")
            return
        if question:
            await channel.send(f"**Kodok Kuestion of the day:** {question}")
        else:
            await channel.send("question e habis bolo, tolong suruh sorin buat refill lol")
    except Exception as e:
        print(f"Error in send_qotd: {e}")
        await reconnect_database()

async def reconnect_database(retries=4):
    global db_pool
    for i in range(retries):
        try:
            if db_pool:
                await db_pool.close()
            db_pool = await init_db()
            if db_pool:
                print("✅ Database reconnected successfully")
                return True
        except Exception as e:
            print(f"Reconnect attempt {i+1} failed: {e}")
            await asyncio.sleep(2 ** i)
    print("❌ Failed to reconnect to database after retries")
    return False

@scheduler.scheduled_job(CronTrigger(hour=12, minute=57, timezone="Asia/Jakarta"))  # 10 minutes before QOTD
async def wake_database_before_qotd():
    """Wake up the database before QOTD runs"""
    print("🔄 Waking up database for QOTD...")

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            print("✅ Database is awake and ready for QOTD")
        except Exception as e:
            print(f"❌ Database wake-up failed: {e}")
            await reconnect_database()
    else:
        print("❌ No database connection, attempting to reconnect...")
        await reconnect_database()


@scheduler.scheduled_job(CronTrigger(hour=13, minute=0, timezone="Asia/Jakarta"))
async def scheduled_qotd():
    """Scheduled QOTD task that runs after database is awake"""
    print("✅ Running QOTD with awake database...")
    await send_qotd()

@bot.event
async def on_error(event, *args, **kwargs):
    if event == 'on_message':
        message = args[0]
        await handle_command_error(message)
    else:
        print(f"Unhandled error in {event}: {kwargs.get('exception')}")

async def handle_command_error(message):
    error_responses = [
        "Anjir error lagi nih...",
        "Buset server error lagi...",
        "Duh error lagi, mungkin lagi ada hantu...",
    ]
    await message.channel.send(random.choice(error_responses))



@bot.event
async def on_ready():
    print("Bot is online.")
    bot.loop.create_task(start_ping_server())

    global db_pool, message_processor_task

    # Try to initialize database with retries
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if db_pool is None:
                db_pool = await init_db()

            if db_pool:
                print("Database connected.")
                scheduler.start()
                break
            else:
                print(f"Database connection failed (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(5)  # Wait before retrying
        except Exception as e:
            print(f"Database connection error: {e}")
            if attempt == max_retries - 1:
                print("Failed to connect to database after multiple attempts")
            else:
                await asyncio.sleep(5)

    message_processor_task = asyncio.create_task(message_processor())
    ensure_tts_worker_started()
    print(f"Logged in as {bot.user}")


@scheduler.scheduled_job("interval", minutes=5)
async def clear_sessions_task():
    await clear_expired_sessions()


@bot.command(name="question")
async def test_qotd(ctx):
    """Test the Question of the Day manually"""
    if db_pool is None:
        await ctx.send("Database not connected yet. Please try again in a moment.")
        return

    question = await get_qotd()
    if question:
        await ctx.send(f"**Kodok Kuestion of the day (Test):** {question}")
    else:
        await ctx.send("No more questions left in the database, bro 😭")

@bot.command(name="joinvc")
async def join_vc(ctx):
    """Join or move to the user's voice channel."""
    global tts_voice_client

    if not ctx.author.voice:
        await ctx.send("You’re not in a voice channel, bruh.")
        return

    channel = ctx.author.voice.channel

    # If already connected, move instead of reconnecting
    if ctx.voice_client:
        if ctx.voice_client.channel == channel:
            await ctx.send(f"Already in {channel.name}, chill 😎")
            return
        else:
            await ctx.voice_client.move_to(channel)
            tts_voice_client = ctx.voice_client
            await ctx.send(f"Moved to {channel.name} 🐸")
            return

    # Otherwise, connect normally
    tts_voice_client = await channel.connect()
    await ctx.send(f"🐸 Joined {channel.name} and ready to speak!")


@bot.command(name="starttts")
async def start_tts(ctx, member: discord.Member):
    """Start reading messages from the specified user. Auto-joins the caller's VC."""
    global active_tts_user, last_tts_activity, tts_voice_client

    if not ctx.author.voice:
        await ctx.send("masuk VC dulu bro, baru aku ikut.")
        return

    channel = ctx.author.voice.channel

    # Already connected somewhere — move if needed, otherwise stay.
    if ctx.voice_client:
        if ctx.voice_client.channel != channel:
            try:
                await ctx.voice_client.move_to(channel)
            except Exception as e:
                print(f"[tts] move_to failed: {e}")
                await ctx.send("ga bisa pindah VC bro, coba lagi")
                return
        tts_voice_client = ctx.voice_client
    else:
        try:
            tts_voice_client = await channel.connect()
        except Exception as e:
            print(f"[tts] connect failed: {e}")
            await ctx.send(f"ga bisa join VC bro: {e}")
            return

    active_tts_user = member.id
    last_tts_activity = time.time()
    await ctx.send(f"🐸 Joined **{channel.name}** and reading messages from **{member.display_name}** from any channel.")

@bot.command(name="stoptts")
async def stop_tts(ctx):
    """Stop reading messages, drain the TTS queue, and leave VC."""
    global active_tts_user, tts_voice_client

    active_tts_user = None

    # Drain any pending queued TTS so we don't keep talking after stop
    drained = 0
    while not tts_message_queue.empty():
        try:
            tts_message_queue.get_nowait()
            tts_message_queue.task_done()
            drained += 1
        except asyncio.QueueEmpty:
            break

    if ctx.voice_client:
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        tts_voice_client = None
        await ctx.send("🕳️ Left the VC and stopped TTS.")
    else:
        await ctx.send("I'm not connected to any voice channel.")


# Add this function to get a random user with an activity
async def get_random_user_with_activity(guild):
    """Get a random user who has a current activity (game, music, etc)"""
    users_with_activities = []

    print(f"🔍 Scanning {len(guild.members)} members in guild: {guild.name}")

    for member in guild.members:
        # Skip bots and offline users
        if member.bot:
            continue

        if member.status == discord.Status.offline:
            continue

        # Check if user has any activities
        if member.activities:
            valid_activity_found = False

            for activity in member.activities:
                # Filter out custom statuses and focus on meaningful activities
                if (isinstance(activity, discord.Spotify) or
                        isinstance(activity, discord.Game) or
                        isinstance(activity, discord.Streaming) or
                        (hasattr(activity, 'type') and
                         activity.type in [discord.ActivityType.playing,
                                           discord.ActivityType.listening,
                                           discord.ActivityType.streaming,
                                           discord.ActivityType.watching])):

                    # Additional filtering for custom statuses
                    if (isinstance(activity, discord.CustomActivity) or
                            (hasattr(activity, 'type') and activity.type == discord.ActivityType.custom)):
                        continue  # Skip custom statuses

                    valid_activity_found = True
                    break

            if valid_activity_found:
                users_with_activities.append(member)

    print(f"📊 Found {len(users_with_activities)} users with valid activities")

    if users_with_activities:
        chosen_user = random.choice(users_with_activities)
        print(f"🎯 Selected user: {chosen_user.display_name}")
        return chosen_user

    print("❌ No users with valid activities found")
    return None

# Add this function to describe the activity
def describe_activity(member):
    """Generate a description of the user's activities, excluding custom statuses"""
    if not member.activities:
        return f"{member.display_name} is doing nothing interesting"

    activities_info = []
    for activity in member.activities:
        # Skip custom statuses
        if (isinstance(activity, discord.CustomActivity) or
                (hasattr(activity, 'type') and activity.type == discord.ActivityType.custom)):
            continue

        if isinstance(activity, discord.Spotify):
            activities_info.append(f"listening to {activity.title} by {activity.artist}")
        elif isinstance(activity, discord.Game):
            activities_info.append(f"playing {activity.name}")
        elif isinstance(activity, discord.Streaming):
            activities_info.append(f"streaming {activity.name} on {activity.platform}")
        elif activity.type == discord.ActivityType.watching:
            activities_info.append(f"watching {activity.name}")
        elif activity.type == discord.ActivityType.listening:
            activities_info.append(f"listening to {activity.name}")
        elif activity.type == discord.ActivityType.playing:
            activities_info.append(f"playing {activity.name}")
        # Skip any other activities that might be custom statuses

    if not activities_info:
        return f"{member.display_name} is doing nothing interesting"

    return f"{member.display_name} is {', and '.join(activities_info)}"

# Add this function to generate commentary
async def generate_activity_commentary(activity_description, user):
    """Generate witty commentary about the user's activity"""
    prompt = f"A user is {activity_description}. Generate a short, witty, sarcastic commentary about this in Indonesian mixed with English. Keep it under 2 sentences and make it funny."

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": PERSONALITY},
                {"role": "user", "content": prompt}
            ],
            stream=False
        )
        # Add user mention to the response
        return f"{user.mention} {response.choices[0].message.content.strip()}"
    except Exception as e:
        return f"{user.mention} Waduh, liat nih orang {activity_description}... interesting choice! 🐸"

# Add this scheduled job (example: runs every 2 hours)

TARGET_CHANNEL_ID = 1333665831200100353
WEEKLY_DIGEST_CHANNEL_ID = 1333665831200100353


@scheduler.scheduled_job(CronTrigger(hour='*/2', minute=0, timezone="Asia/Jakarta"))
async def random_activity_commentary():
    try:
        if not bot.guilds:
            return

        guild = random.choice(bot.guilds)
        user = await get_random_user_with_activity(guild)

        if not user:
            return

        activity_description = describe_activity(user)
        # Pass the user to the commentary function
        commentary = await generate_activity_commentary(activity_description, user)

        # Send to specific channel
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if target_channel:
            await target_channel.send(commentary)
            print(f"✅ Activity commentary sent for {user.display_name}")

    except Exception as e:
        print(f"Error in activity commentary: {e}")

@bot.command(name="stalk324")
async def stalk_command(ctx):
    """Manually trigger activity commentary"""
    try:
        print(f"🕵️ Stalk command triggered in {ctx.guild.name}")
        user = await get_random_user_with_activity(ctx.guild)

        if not user:
            await ctx.send("Ga ada yang lagi doing anything interesting nih... semua pada idle 😴")
            return

        # Debug: Show what activities the user has
        print(f"📋 Activities for {user.display_name}:")
        for i, activity in enumerate(user.activities):
            print(f"  {i + 1}. {activity.name} (type: {type(activity).__name__})")

        activity_description = describe_activity(user)
        print(f"📝 Generated description: {activity_description}")

        # Pass the user to the commentary function
        commentary = await generate_activity_commentary(activity_description, user)

        await ctx.send(commentary)

    except Exception as e:
        await ctx.send("Waduh error lagi nih, coba lagi nanti...")
        print(f"❌ Stalk command error: {e}")
        import traceback
        traceback.print_exc()

# Update the daily_stalk function to pass the user to generate_activity_commentary
@scheduler.scheduled_job(CronTrigger(hour=19, minute=0, timezone="Asia/Jakarta"))  # 7 PM Jakarta time
async def daily_stalk():
    """Randomly stalk one person every day at 7 PM"""
    try:
        print("🕔 7 PM - Time for daily stalk!")

        # Get a random guild (server) the bot is in
        if not bot.guilds:
            print("❌ No guilds available")
            return

        guild = random.choice(bot.guilds)
        print(f"🎯 Selected guild: {guild.name}")

        user = await get_random_user_with_activity(guild)

        if not user:
            print("❌ No users with activities found for daily stalk")
            return

        # Debug: Show what activities the user has
        print(f"📋 Activities for {user.display_name}:")
        for i, activity in enumerate(user.activities):
            print(f"  {i + 1}. {activity.name} (type: {type(activity).__name__})")

        activity_description = describe_activity(user)
        print(f"📝 Generated description: {activity_description}")

        # Pass the user to the commentary function
        commentary = await generate_activity_commentary(activity_description, user)

        # Send to a specific channel or random channel
        TARGET_CHANNEL_ID = 1333665831200100353  # Replace with your desired channel ID
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)

        if target_channel:
            await target_channel.send(commentary)
            print(f"✅ Daily stalk completed for {user.display_name}")
        else:
            print(f"❌ Could not find target channel {TARGET_CHANNEL_ID}")

    except Exception as e:
        print(f"❌ Daily stalk error: {e}")
        import traceback
        traceback.print_exc()


def cleanup_tts_file_sync(file_path, error=None):
    """Sync cleanup for FFmpeg's `after` callback (which fires from a non-async thread)."""
    if error:
        print(f"[tts] playback error: {error}")
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        print(f"[tts] error cleaning up file: {e}")


async def cleanup_tts_file(file_path, error=None):
    """Clean up temporary TTS files after playback"""
    if error:
        print(f"TTS playback error: {error}")

    # Wait a bit then clean up
    await asyncio.sleep(1)

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Cleaned up TTS file: {file_path}")
    except Exception as e:
        print(f"Error cleaning up TTS file: {e}")
@scheduler.scheduled_job("interval", minutes=1)
async def tts_inactivity_check():
    global last_tts_activity, tts_voice_client, active_tts_user

    if tts_voice_client and active_tts_user:
        if time.time() - last_tts_activity > 15 * 60:  # 15 minutes
            try:
                await tts_voice_client.disconnect()
                print("🕒 Auto-disconnected from VC due to inactivity.")
            except Exception as e:
                print(f"Error disconnecting TTS VC: {e}")
            finally:
                tts_voice_client = None
                active_tts_user = None


# =====================================================
# HANGOUT FEATURE
# =====================================================

HANGOUT_REACTION = "✅"
DAY_NAMES_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
MONTH_NAMES_ID = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
                  "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def format_event_date(date_obj, time_obj=None):
    """Format a date (and optional time) like 'Rabu, 20 Mei 2026, jam 19:00'."""
    if isinstance(date_obj, str):
        date_obj = datetime.fromisoformat(date_obj).date()
    day_name = DAY_NAMES_ID[date_obj.weekday()]
    base = f"{day_name}, {date_obj.day} {MONTH_NAMES_ID[date_obj.month - 1]} {date_obj.year}"
    if time_obj is None:
        return base
    if isinstance(time_obj, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                time_obj = datetime.strptime(time_obj, fmt).time()
                break
            except Exception:
                pass
        else:
            return base
    return f"{base}, jam {time_obj.strftime('%H:%M')}"


def parse_event_time(s):
    """Parse 'HH:MM' or 'HH:MM:SS' string to datetime.time. Returns None on failure."""
    if not s:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except Exception:
            pass
    return None


def format_hangout_summary(h):
    """Format a hangout row/dict as a Discord message body."""
    status_prefix = ""
    if h.get("status") == "cancelled":
        status_prefix = "❌ **CANCELLED** ❌\n\n"
    location = h.get("location") or "(belum ditentuin)"
    description = h.get("description") or "(no description)"
    return (
        f"{status_prefix}"
        f"📅 **Hangout #{h['id']}**\n"
        f"**Tanggal:** {format_event_date(h['event_date'], h.get('event_time'))}\n"
        f"**Tempat:** {location}\n"
        f"**Acara:** {description}\n\n"
        f"React with {HANGOUT_REACTION} kalo mau ikut!"
    )


async def deepseek_extract_hangout(user_message):
    """Extract hangout details from a casual user message. Returns dict."""
    today = datetime.now().date()
    system_prompt = (
        "You extract hangout/event details from a user's casual message "
        "(Indonesian, English, or mixed) and return STRICT JSON only.\n\n"
        f"Today's date is {today.isoformat()} (a {DAY_NAMES_ID[today.weekday()]}).\n\n"
        "Output JSON with these exact fields:\n"
        "- \"valid\": boolean. true ONLY if the message clearly proposes a hangout/meetup "
        "with at least a date AND (a location or activity).\n"
        "- \"event_date\": ISO date string YYYY-MM-DD, or null. If the user says \"tanggal X\" "
        "with no month, pick the NEXT occurrence: if X >= today's day-of-month, use this month; "
        "otherwise next month. \"besok\"=tomorrow, \"lusa\"=day after tomorrow, "
        "\"minggu depan\"=same weekday next week.\n"
        "- \"event_time\": time string \"HH:MM\" (24-hour), or null if not specified. "
        "Indonesian time-of-day hints: \"pagi\"=morning (06-11), \"siang\"=midday (11-15), "
        "\"sore\"=afternoon (15-18), \"malam\"=evening (18-22). "
        "\"jam 7 malam\"=19:00. \"jam 6 sore\"=18:00. \"jam 7 pagi\"=07:00. "
        "\"after maghrib\"=18:00. If user only says \"jam 7\" with no qualifier and "
        "context suggests a hangout, default to evening (19:00).\n"
        "- \"location\": short string like \"Galaxy Mall\", or null.\n"
        "- \"description\": short summary of activity in casual ID/EN, or null.\n"
        "- \"reason\": brief string.\n\n"
        "Return ONLY the JSON object, no markdown fences, no commentary."
    )
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            stream=False,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[hangout extract] error: {e}")
        return {"valid": False, "reason": f"extraction failed: {e}"}


async def deepseek_match_hangout(user_message, hangouts, want_updates=False):
    """Match a natural-language reference to one of the active hangouts."""
    today = datetime.now().date()
    hangouts_summary = [
        {
            "id": h["id"],
            "event_date": h["event_date"].isoformat() if hasattr(h["event_date"], "isoformat") else str(h["event_date"]),
            "event_time": h["event_time"].strftime("%H:%M") if h.get("event_time") else None,
            "location": h.get("location"),
            "description": h.get("description"),
        }
        for h in hangouts
    ]
    update_block = ""
    if want_updates:
        update_block = (
            "\n- \"updates\": object with any fields the user wants to change. "
            "Allowed keys: \"event_date\" (YYYY-MM-DD), \"event_time\" (HH:MM 24h), "
            "\"location\" (string), \"description\" (string). "
            "Only include keys the user explicitly changed."
        )
    system_prompt = (
        "You match a user's natural-language reference to one of the existing hangouts "
        "and return STRICT JSON only.\n\n"
        f"Today is {today.isoformat()}.\n\n"
        f"Active hangouts:\n{json.dumps(hangouts_summary, indent=2)}\n\n"
        f"User message:\n\"{user_message}\"\n\n"
        "Output JSON:\n"
        "- \"status\": \"match\" | \"ambiguous\" | \"not_found\"\n"
        "- \"match_id\": integer id, or null\n"
        "- \"candidate_ids\": list of ids if ambiguous, else []"
        f"{update_block}\n"
        "- \"reason\": brief string.\n\n"
        "Return ONLY the JSON object."
    )
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            stream=False,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[hangout match] error: {e}")
        return {"status": "not_found", "match_id": None, "candidate_ids": [], "reason": f"match failed: {e}"}


# ----- DB helpers for hangouts -----

async def db_create_hangout(guild_id, channel_id, creator_id, event_date, event_time, location, description):
    if not await ensure_db_pool():
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO hangouts (guild_id, channel_id, creator_id, event_date, event_time, location, description)
               VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
            guild_id, channel_id, creator_id, event_date, event_time, location, description,
        )
        return row["id"] if row else None


async def db_set_hangout_message(hangout_id, message_id):
    if not await ensure_db_pool():
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE hangouts SET message_id=$1 WHERE id=$2", message_id, hangout_id)


async def db_get_hangout(hangout_id):
    if not await ensure_db_pool():
        return None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM hangouts WHERE id=$1", hangout_id)


async def db_list_active_hangouts(guild_id):
    if not await ensure_db_pool():
        return []
    today = datetime.now().date()
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """SELECT * FROM hangouts
               WHERE guild_id=$1 AND status='active' AND event_date >= $2
               ORDER BY event_date ASC""",
            guild_id, today,
        )


async def db_update_hangout(hangout_id, fields):
    if not await ensure_db_pool() or not fields:
        return False
    set_clauses = []
    values = []
    for i, (k, v) in enumerate(fields.items(), start=1):
        set_clauses.append(f"{k}=${i}")
        values.append(v)
    values.append(hangout_id)
    sql = f"UPDATE hangouts SET {', '.join(set_clauses)} WHERE id=${len(values)}"
    async with db_pool.acquire() as conn:
        await conn.execute(sql, *values)
    return True


async def db_cancel_hangout(hangout_id):
    if not await ensure_db_pool():
        return False
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE hangouts SET status='cancelled' WHERE id=$1 AND status='active'",
            hangout_id,
        )
    return True


# ----- Command handlers -----

async def handle_hangout_create(message):
    body = message.content.strip()[len("kodok sayang"):].strip()
    if not body:
        await message_queue.put((message, "kodok sayang juga, tapi kasitau detail dong, tanggal sama tempat mau kemana"))
        return

    extracted = await deepseek_extract_hangout(body)
    if not extracted.get("valid") or not extracted.get("event_date"):
        await message_queue.put((message, "hmm ga jelas detailnya bro, kasitau tanggal sama tempat mau kemana 🐸"))
        return

    try:
        event_date = datetime.fromisoformat(extracted["event_date"]).date()
    except Exception:
        await message_queue.put((message, "tanggalnya aneh bro coba lagi"))
        return

    if event_date < datetime.now().date():
        await message_queue.put((message, f"loh tanggal {event_date} kan udah lewat bro 💀"))
        return

    event_time = parse_event_time(extracted.get("event_time"))

    hangout_id = await db_create_hangout(
        guild_id=message.guild.id if message.guild else 0,
        channel_id=message.channel.id,
        creator_id=message.author.id,
        event_date=event_date,
        event_time=event_time,
        location=extracted.get("location"),
        description=extracted.get("description"),
    )
    if hangout_id is None:
        await message_queue.put((message, "waduh DB error, coba lagi nanti"))
        return

    hangout = {
        "id": hangout_id,
        "event_date": event_date,
        "event_time": event_time,
        "location": extracted.get("location"),
        "description": extracted.get("description"),
        "status": "active",
    }
    body_text = "okay man noted! 🐸\n\n" + format_hangout_summary(hangout)
    try:
        sent = await message.channel.send(body_text)
        try:
            await sent.add_reaction(HANGOUT_REACTION)
        except Exception as e:
            print(f"[hangout] failed to add reaction: {e}")
        await db_set_hangout_message(hangout_id, sent.id)
    except Exception as e:
        print(f"[hangout] failed to send announcement: {e}")
        await message_queue.put((message, "kelar nyimpen tapi ga bisa kirim announcement, sori bro"))


async def handle_hangout_update(message):
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return
    body = message.content.strip()[len("kodok update hangout"):].strip()
    if not body:
        await message_queue.put((message, "update hangout yang mana, kasitau bro"))
        return

    hangouts = await db_list_active_hangouts(message.guild.id)
    if not hangouts:
        await message_queue.put((message, "ga ada hangout aktif sekarang, ga ada yang bisa diupdate"))
        return

    result = await deepseek_match_hangout(body, [dict(h) for h in hangouts], want_updates=True)
    status = result.get("status")
    if status == "not_found":
        await message_queue.put((message, "ga nemu hangout itu di list, coba cek `kodok jadwal apa aja`"))
        return
    if status == "ambiguous":
        ids = ", ".join(f"#{i}" for i in (result.get("candidate_ids") or []))
        await message_queue.put((message, f"yang mana sih, ada {ids}? specifikin dong"))
        return

    hangout_id = result.get("match_id")
    if hangout_id is None:
        await message_queue.put((message, "bingung gw, hangout mana yang dimaksud"))
        return

    updates = result.get("updates") or {}
    clean = {}
    if "event_date" in updates:
        try:
            d = datetime.fromisoformat(updates["event_date"]).date()
            if d < datetime.now().date():
                await message_queue.put((message, "tanggal baru udah lewat anjir"))
                return
            clean["event_date"] = d
        except Exception:
            pass
    if "event_time" in updates:
        t = parse_event_time(updates["event_time"])
        if t is not None:
            clean["event_time"] = t
    for k in ("location", "description"):
        if k in updates and isinstance(updates[k], str):
            clean[k] = updates[k]

    if not clean:
        await message_queue.put((message, "mau update apa coba? gajelas"))
        return

    await db_update_hangout(hangout_id, clean)

    updated = await db_get_hangout(hangout_id)
    if updated and updated["message_id"]:
        try:
            channel = bot.get_channel(updated["channel_id"])
            if channel:
                msg = await channel.fetch_message(updated["message_id"])
                await msg.edit(content="okay man noted! 🐸 (updated)\n\n" + format_hangout_summary(dict(updated)))
        except Exception as e:
            print(f"[hangout] failed to edit announcement: {e}")

    await message_queue.put((message, f"udah di-update bro, hangout #{hangout_id} 🐸"))


async def handle_hangout_cancel(message):
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return
    body = message.content.strip()[len("kodok cancel hangout"):].strip()
    if not body:
        await message_queue.put((message, "cancel yang mana, kasitau"))
        return

    hangouts = await db_list_active_hangouts(message.guild.id)
    if not hangouts:
        await message_queue.put((message, "ga ada hangout aktif sekarang"))
        return

    result = await deepseek_match_hangout(body, [dict(h) for h in hangouts], want_updates=False)
    status = result.get("status")
    if status == "not_found":
        await message_queue.put((message, "hangout apa anjir gada di list"))
        return
    if status == "ambiguous":
        ids = ", ".join(f"#{i}" for i in (result.get("candidate_ids") or []))
        await message_queue.put((message, f"yang mana sih, ada {ids}? specifikin dong"))
        return

    hangout_id = result.get("match_id")
    if hangout_id is None:
        await message_queue.put((message, "bingung gw, hangout mana yang dicancel"))
        return

    await db_cancel_hangout(hangout_id)

    updated = await db_get_hangout(hangout_id)
    if updated and updated["message_id"]:
        try:
            channel = bot.get_channel(updated["channel_id"])
            if channel:
                msg = await channel.fetch_message(updated["message_id"])
                await msg.edit(content=format_hangout_summary(dict(updated)))
        except Exception as e:
            print(f"[hangout] failed to edit announcement on cancel: {e}")

    await message_queue.put((message, f"hangout #{hangout_id} udah di-cancel bro 💀"))


async def handle_hangout_list(message):
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return
    hangouts = await db_list_active_hangouts(message.guild.id)
    if not hangouts:
        await message_queue.put((message, "lagi gada hangout aktif bolo, sepi banget hidupmu"))
        return
    lines = ["📋 **Hangout aktif:**"]
    for h in hangouts:
        loc = h["location"] or "(no location)"
        desc = h["description"] or "(no description)"
        lines.append(f"`#{h['id']}` — {format_event_date(h['event_date'], h.get('event_time'))} — {loc} — {desc}")
    await message_queue.put((message, "\n".join(lines)))


# ----- Day-before reminder -----

async def send_hangout_reminder(hangout, mark_reminded=True, prefix="⏰ **Reminder: hangout besok!**"):
    """Send a reminder for one hangout. Returns True on success."""
    try:
        channel = bot.get_channel(hangout["channel_id"])
        if not channel or not hangout["message_id"]:
            return False
        msg = await channel.fetch_message(hangout["message_id"])
        attendees = []
        for reaction in msg.reactions:
            if str(reaction.emoji) == HANGOUT_REACTION:
                async for user in reaction.users():
                    if not user.bot:
                        attendees.append(user.mention)
                break
        mentions = " ".join(attendees) if attendees else "(belum ada yang react ikut)"
        loc = hangout["location"] or "(no location)"
        desc = hangout["description"] or "(no description)"
        await channel.send(
            f"{prefix}\n"
            f"{format_event_date(hangout['event_date'], hangout.get('event_time'))} — {loc} — {desc}\n\n"
            f"yo {mentions}, jangan lupa siap2 🐸"
        )
        if mark_reminded and await ensure_db_pool():
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE hangouts SET reminded=TRUE WHERE id=$1", hangout["id"])
        return True
    except Exception as e:
        print(f"[hangout reminder] failed for #{hangout['id']}: {e}")
        return False


@scheduler.scheduled_job(CronTrigger(hour=9, minute=0, timezone="Asia/Jakarta"))
async def daily_hangout_reminders():
    """Send a heads-up the day before each hangout."""
    if not await ensure_db_pool():
        return
    tomorrow = (datetime.now() + timedelta(days=1)).date()
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM hangouts
                   WHERE status='active' AND event_date=$1 AND reminded=FALSE""",
                tomorrow,
            )
        for h in rows:
            await send_hangout_reminder(dict(h), mark_reminded=True)
    except Exception as e:
        print(f"[hangout reminder] outer error: {e}")


# ===== Weekly digest =====

async def generate_digest_commentary(hangouts, swear_rows):
    """Snarky weekly commentary from Kodok based on the week's stats."""
    hangout_part = (
        f"there are {len(hangouts)} upcoming hangout(s)" if hangouts
        else "there are no upcoming hangouts"
    )
    swear_part = (
        f"top swearer has {swear_rows[0]['count']} swears total"
        if swear_rows else "no one has been tracked swearing"
    )
    prompt = (
        f"You're writing the weekly Discord digest commentary for the friend group. "
        f"Context: {hangout_part}; {swear_part}. "
        f"Generate 1-3 SHORT sentences of snarky, casual commentary about the week's vibe in Indonesian/English mix. "
        f"Be entertaining and a bit roasty. Do NOT repeat the stats numerically. Do NOT use hashtags. "
        f"Do NOT include a greeting or sign-off. Just the commentary."
    )
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": PERSONALITY},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[weekly digest] commentary error: {e}")
        return "begitulah minggu ini, see you next week \U0001F438"


@scheduler.scheduled_job(CronTrigger(day_of_week="sun", hour=12, minute=0, timezone="Asia/Jakarta"))
async def weekly_digest():
    """Every Sunday 12:00 Jakarta: upcoming hangouts + swear leaderboard + Kodok's commentary."""
    channel = bot.get_channel(WEEKLY_DIGEST_CHANNEL_ID)
    if not channel:
        print(f"[weekly digest] channel {WEEKLY_DIGEST_CHANNEL_ID} not found")
        return
    guild = channel.guild
    if not guild:
        print("[weekly digest] channel has no guild")
        return

    hangouts = await db_list_active_hangouts(guild.id)
    hangout_lines = []
    for h in hangouts:
        loc = h["location"] or "(no location)"
        desc = h["description"] or "(no description)"
        hangout_lines.append(
            f"`#{h['id']}` — {format_event_date(h['event_date'], h.get('event_time'))} — {loc} — {desc}"
        )

    swear_rows = await db_swear_leaderboard(guild.id, limit=3)
    swear_lines = []
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    for i, row in enumerate(swear_rows):
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"<unknown {row['user_id']}>"
        swear_lines.append(f"{medals[i]} **{name}** — {row['count']} swears")

    parts = ["\U0001F4F0 **Weekly Kodok Digest** \U0001F4F0", ""]
    parts.append("\U0001F4C5 **Upcoming Hangouts:**")
    if hangout_lines:
        parts.extend(hangout_lines)
    else:
        parts.append("_ga ada hangout aktif, sepi minggu ini_")
    parts.append("")
    parts.append("\U0001F92C **Top 3 Mulut Kotor:**")
    if swear_lines:
        parts.extend(swear_lines)
    else:
        parts.append("_ga ada yang ngomong kasar, suspicious banget_")
    parts.append("")

    structured = "\n".join(parts)
    commentary = await generate_digest_commentary(list(hangouts), list(swear_rows))
    final = f"{structured}\n{commentary}"

    try:
        await channel.send(final)
        print(f"[weekly digest] sent ({len(hangouts)} hangouts, {len(swear_rows)} swearers)")
    except Exception as e:
        print(f"[weekly digest] send failed: {e}")


async def handle_hangout_test_reminder(message):
    """Manually fire reminder(s) for testing. Doesn't mark reminded=TRUE."""
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return
    body = message.content.strip()[len("kodok test reminder"):].strip()

    hangouts = await db_list_active_hangouts(message.guild.id)
    if not hangouts:
        await message_queue.put((message, "ga ada hangout aktif buat di-test bro"))
        return

    test_prefix = "🧪 **Test reminder** (this is a test, ga di-mark sebagai reminded)"

    if not body:
        # No reference: fire for all active hangouts
        sent = 0
        for h in hangouts:
            ok = await send_hangout_reminder(dict(h), mark_reminded=False, prefix=test_prefix)
            if ok:
                sent += 1
        await message_queue.put((message, f"udah test reminder {sent}/{len(hangouts)} hangout aktif 🐸"))
        return

    # With description: match a specific hangout
    result = await deepseek_match_hangout(body, [dict(h) for h in hangouts], want_updates=False)
    status = result.get("status")
    if status == "not_found":
        await message_queue.put((message, "ga nemu hangout itu di list"))
        return
    if status == "ambiguous":
        ids = ", ".join(f"#{i}" for i in (result.get("candidate_ids") or []))
        await message_queue.put((message, f"yang mana sih, ada {ids}? specifikin"))
        return

    hangout_id = result.get("match_id")
    if hangout_id is None:
        await message_queue.put((message, "bingung gw, hangout mana"))
        return
    hangout = await db_get_hangout(hangout_id)
    if not hangout:
        await message_queue.put((message, "hangout udah ga ada"))
        return
    ok = await send_hangout_reminder(dict(hangout), mark_reminded=False, prefix=test_prefix)
    if not ok:
        await message_queue.put((message, "test reminder gagal kirim, cek log"))



# =====================================================
# SWEAR JAR FEATURE
# =====================================================

SWEAR_MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
swear_locks = defaultdict(asyncio.Lock)

# Channels where milestone announcements are suppressed (counter still increments,
# so the leaderboard stays accurate — we just don't disrupt the channel with snark).
# To get a channel ID: User Settings → Advanced → enable Developer Mode,
# then right-click the channel → Copy Channel ID. Add as integers below.
SWEAR_MILESTONE_MUTED_CHANNELS = {
    1308422878802350171,  # #vent
    1306684687032258580,  # #panic
}

# Word stems with optional inflections. Used inside \b...\b boundaries.
_SWEAR_STEMS = [
    # English - Medium
    r"shit(?:s|ty|tier|tiest|ting|ted|head|heads|hole|holes|stain|y|tin|tin')?",
    r"bitch(?:es|y|ed|ing|in|in')?",
    r"ass",
    r"asshole(?:s)?",
    r"dick(?:s|head|heads)?",
    r"piss(?:ed|ing|es|er|y)?",
    r"bastard(?:s)?",
    r"prick(?:s)?",
    # Indonesian - Medium
    r"anjing(?:nya)?",
    r"anjir+",
    r"anjg",
    r"asu+",
    r"bajingan(?:s)?",
    r"bangsa[td]",
    r"tai",
    r"taik",
    r"taek",
    r"jancok",
    r"jancuk(?:s)?",
    r"cok",
    r"cuk",
    # English - Hard
    r"fuck(?:ing|in|ed|er|ers|s|wit|tard|boy|able|in')?",
    r"motherfucker(?:s)?",
    r"fck",
    r"fuk",
    r"cunt(?:s)?",
    r"cock(?:s|sucker|head)?",
    r"pussy",
    r"pussies",
    r"twat(?:s)?",
    r"wank(?:er|ers|ed|ing|y)?",
    r"whore(?:s)?",
    r"slut(?:s|ty|tier)?",
    # Indonesian - Hard
    r"kontol+",
    r"memek(?:s)?",
    r"ngentot+",
    r"ngentod",
    r"pepek(?:s)?",
    r"kimak+",
    r"cukimak",
    r"pantek(?:s)?",
]

SWEAR_REGEX = re.compile(r"\b(?:" + "|".join(_SWEAR_STEMS) + r")\b", re.IGNORECASE)


def count_swears(text):
    """Return the number of swear-word occurrences in text."""
    if not text:
        return 0
    return len(SWEAR_REGEX.findall(text))


def hit_milestone(old_count, new_count):
    """Return the highest milestone crossed in this jump, else None."""
    crossed = [m for m in SWEAR_MILESTONES if old_count < m <= new_count]
    return max(crossed) if crossed else None


# ----- DB helpers for swear jar -----

async def db_increment_swear(guild_id, user_id, increment):
    """Atomically increment a user's swear count. Returns new total."""
    if not await ensure_db_pool():
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO swear_counts (guild_id, user_id, count)
               VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, user_id)
               DO UPDATE SET count = swear_counts.count + EXCLUDED.count,
                             last_updated = NOW()
               RETURNING count""",
            guild_id, user_id, increment,
        )
        return row["count"] if row else None


async def db_swear_leaderboard(guild_id, limit=3):
    if not await ensure_db_pool():
        return []
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """SELECT user_id, count FROM swear_counts
               WHERE guild_id=$1
               ORDER BY count DESC
               LIMIT $2""",
            guild_id, limit,
        )


async def db_swear_counts_for_users(guild_id, user_ids):
    """Return {user_id: count} for the given users, defaulting missing ones to 0."""
    if not await ensure_db_pool() or not user_ids:
        return {uid: 0 for uid in user_ids}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_id, count FROM swear_counts
               WHERE guild_id=$1 AND user_id = ANY($2::bigint[])""",
            guild_id, list(user_ids),
        )
    counts = {row["user_id"]: row["count"] for row in rows}
    return {uid: counts.get(uid, 0) for uid in user_ids}


# ----- LLM snark for milestones -----

async def generate_swear_milestone_snark(member, milestone, total_count):
    """Generate just the snark line (name + count are in the structured headline above it)."""
    prompt = (
        f"The user '{member.display_name}' just hit a swear milestone in this Discord server. "
        f"Generate ONE SHORT (1-2 sentences) snarky, celebratory roast in casual Indonesian/English mix. "
        f"Be playful and a bit sarcastic. Don't include hashtags. "
        f"DO NOT mention their name (it appears separately). "
        f"DO NOT mention the milestone number (it appears separately)."
    )
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": PERSONALITY},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[swear jar] LLM error: {e}")
        return "santai dikit bro, mulut e kotor banget \U0001F438"


# ----- Per-message processor (fire-and-forget) -----

async def process_swear_count(message):
    """Count swears in this message, increment user's tally, fire snark on milestone."""
    if message.author.bot or message.guild is None:
        return
    n = count_swears(message.content)
    if n == 0:
        return

    user_key = (message.guild.id, message.author.id)
    async with swear_locks[user_key]:
        new_count = await db_increment_swear(message.guild.id, message.author.id, n)
        if new_count is None:
            return
        old_count = new_count - n
        milestone = hit_milestone(old_count, new_count)
        if milestone is None:
            return
        # Suppress milestone announcement in muted channels (counter is already incremented
        # above, so the leaderboard stays accurate).
        if message.channel.id in SWEAR_MILESTONE_MUTED_CHANNELS:
            channel_name = getattr(message.channel, "name", message.channel.id)
            print(f"[swear jar] suppressed milestone {milestone} for {message.author.display_name} in muted channel #{channel_name}")
            return
        try:
            snark = await generate_swear_milestone_snark(message.author, milestone, new_count)
            await message.channel.send(
                f"\U0001F389 {message.author.mention} just hit **{milestone}** total swears!\n{snark}"
            )
        except Exception as e:
            print(f"[swear jar] failed to send milestone snark: {e}")


# ----- Leaderboard handler -----

async def handle_swear_leaderboard(message):
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return
    rows = await db_swear_leaderboard(message.guild.id, limit=3)
    if not rows:
        await message_queue.put((message, "ga ada yang ngomong kasar di sini... boring banget hidup kalian"))
        return
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = ["**\U0001F92C Top 3 Mulut Kotor:**"]
    for i, row in enumerate(rows):
        member = message.guild.get_member(row["user_id"])
        name = member.display_name if member else f"<unknown user {row['user_id']}>"
        lines.append(f"{medals[i]} **{name}** — {row['count']} swears")
    await message_queue.put((message, "\n".join(lines)))


async def handle_swear_count(message):
    """Show the swear count for the message author, or a mentioned user."""
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return

    # If someone is mentioned (other than the author), show their count instead.
    target = next((m for m in message.mentions if not m.bot and m.id != message.author.id), None)
    target = target or message.author

    counts = await db_swear_counts_for_users(message.guild.id, [target.id])
    count = counts.get(target.id, 0)

    tier_map = [
        (1,    "mulutmu masih bersih \U0001F607"),
        (10,   "amatir, baru mulai \U0001F642"),
        (50,   "lumayan brutal \U0001F608"),
        (100,  "mulutmu kotor \U0001F480"),
        (250,  "certified menace \U0001F525"),
        (500,  "unhinged \U0001F92C"),
        (10**9, "swearing royalty \U0001F451"),
    ]
    tier = next(t for c, t in tier_map if count < c)

    if target.id == message.author.id:
        prefix = f"{message.author.mention} kamu"
    else:
        prefix = f"**{target.display_name}**"

    await message_queue.put((message, f"{prefix} udah **{count}** swears di server ini. {tier}"))


async def handle_clean_mouth_leaderboard(message):
    """Top 3 LEAST swearers in the server (the opposite of the swear leaderboard)."""
    if message.guild is None:
        await message_queue.put((message, "kerjain di server bro"))
        return
    members = [m for m in message.guild.members if not m.bot]
    if not members:
        await message_queue.put((message, "ga ada member yang bisa di-rank"))
        return
    counts = await db_swear_counts_for_users(message.guild.id, [m.id for m in members])
    ranked = sorted(members, key=lambda m: (counts.get(m.id, 0), m.display_name.lower()))
    top = ranked[:3]
    medals = ["\U0001F607", "\U0001F642", "\U0001F60C"]
    lines = ["**\U0001F47C Top 3 Mulut Bersih:**"]
    for i, member in enumerate(top):
        c = counts.get(member.id, 0)
        lines.append(f"{medals[i]} **{member.display_name}** — {c} swears")
    await message_queue.put((message, "\n".join(lines)))



async def _play_tts_for_message(message):
    """Fire-and-forget: synthesize Gemini TTS and play it on the active voice client."""
    global tts_voice_client
    try:
        print(f"[tts] synthesizing for: {message.content[:80]}")
        wav_bytes = await synthesize_tts_audio(message.content)
        if not wav_bytes:
            print("[tts] synth returned no audio, skipping")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_file:
            temp_path = temp_file.name
            temp_file.write(wav_bytes)

        if not (tts_voice_client and tts_voice_client.is_connected()):
            try:
                os.remove(temp_path)
            except Exception:
                pass
            return

        if tts_voice_client.is_playing():
            # Another message already grabbed the channel; queue is not implemented, just drop.
            try:
                os.remove(temp_path)
            except Exception:
                pass
            return

        audio_source = discord.FFmpegPCMAudio(temp_path)
        tts_voice_client.play(
            audio_source,
            after=lambda e: cleanup_tts_file_sync(temp_path, e),
        )
        print("[tts] playback started")
    except Exception as e:
        print(f"[tts] error in _play_tts_for_message: {e}")


@bot.event
async def on_message(message):
    global active_tts_user, last_tts_activity, tts_voice_client
    if message.author == bot.user:
        return

    # Swear jar: fire-and-forget, doesn't block normal handling
    if not message.author.bot and message.guild:
        asyncio.create_task(process_swear_count(message))

        # 🔥 ADD THIS: Skip command processing in the custom message handler
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return
    # 🔊 TTS functionality (any channel, Gemini TTS, queued)
    if (active_tts_user == message.author.id and
            tts_voice_client and
            tts_voice_client.is_connected() and
            message.content.strip()):

        last_tts_activity = time.time()
        try:
            tts_message_queue.put_nowait(message)
        except asyncio.QueueFull:
            print(f"[tts] queue full ({TTS_QUEUE_MAX}), dropping message")
    history_key = await get_history_key(message)

  
    if message.content.lower() == "okay shut up kodok":
        if history_key in conversation_histories:
            del conversation_histories[history_key]
            await message_queue.put((message, "okay man damn :cold_sweat:"))
        else:
            await message_queue.put((message, "bro i wasn't even talking??? :sob: "))
        return

    # ----- Hangout triggers -----
    content_lower = message.content.lower().strip()
    if content_lower.startswith("kodok sayang"):
        await handle_hangout_create(message)
        return
    if content_lower.startswith("kodok update hangout"):
        await handle_hangout_update(message)
        return
    if content_lower.startswith("kodok cancel hangout"):
        await handle_hangout_cancel(message)
        return
    if content_lower.startswith("kodok test reminder"):
        await handle_hangout_test_reminder(message)
        return
    if content_lower in ("kodok jadwal apa aja", "kodok list hangout", "kodok hangouts"):
        await handle_hangout_list(message)
        return
    if content_lower.startswith("kodok leaderboard swear") or content_lower.startswith("kodok swear leaderboard"):
        await handle_swear_leaderboard(message)
        return
    if content_lower.startswith("kodok leaderboard clean") or content_lower.startswith("kodok clean leaderboard"):
        await handle_clean_mouth_leaderboard(message)
        return
    if (content_lower == "kodok swear"
            or content_lower.startswith("kodok swear ")
            or content_lower.startswith("kodok berapa swear")):
        # Note: "kodok swear leaderboard" already returned above, so we don't need to re-exclude it
        await handle_swear_count(message)
        return

  
    if message.content.lower().startswith("woi kodok"):
        prompt = message.content[len("woi kodok"):].strip()

        if not prompt:
            await message_queue.put((message, f"what kenapa manggil manggil ak tau aku ganteng {BOT_NAME}? 🐸"))
            return

        async with locks[history_key]:  
           
            await add_to_history(history_key, "user", prompt)

            async with message.channel.typing():
                response_data = await ask_deepseek(history_key)
                response = response_data["content"]

          
            await add_to_history(history_key, "assistant", response)

           
            if response_data["error"]:
                del conversation_histories[history_key]

            await message_queue.put ((message, response))
        return  

   
    if history_key in conversation_histories:
        async with locks[history_key]: 
            
            last_timestamp = datetime.fromisoformat(conversation_histories[history_key][-1]['timestamp'])
            if (datetime.now() - last_timestamp).total_seconds() > SESSION_TIMEOUT:
                del conversation_histories[history_key]
                return

           
            await add_to_history(history_key, "user", message.content)

            async with message.channel.typing():
                response_data = await ask_deepseek(history_key)
                response = response_data["content"]

           
            await add_to_history(history_key, "assistant", response)

            if response_data["error"]:
                del conversation_histories[history_key]

            await message_queue.put((message, response))
        return  

   
    add_pattern = r"add (\w+) (-?\d+) (-?\d+) dong"
    add_match = re.match(add_pattern, message.content.lower())
    if add_match:
        name, x, z = add_match.groups()
        x, z = int(x), int(z)
        await add_coordinate(name, x, z)
        await message_queue.put((message, f"ok kontol Coordinate '{name}' added: X={x}, Z={z}"))
        return
   
    delete_pattern = r"delete (\w+) pls"
    delete_match = re.match(delete_pattern, message.content.lower())
    if delete_match:
        name = delete_match.group(1)
        await delete_coordinate(name)
        await message_queue.put((message, f"Coordinate '{name}' deleted. jahat nye.."))
        return

  
    list_pattern = r"coords po o"
    list_match = re.match(list_pattern, message.content.lower())
    if list_match:
        coords = await list_coordinates()
        if not coords:
            await message_queue.put((message, "masih ga ada coords bro??"))
        else:
            coord_list = "\n\n".join([f"{c['name']}: X={c['x']}, Z={c['z']}" for c in coords])
            await message_queue.put((message, f"nyoh:\n{coord_list}"))
        return

    
    rps_pattern = r"i pick (rock|paper|scissors)"
    rps_match = re.match(rps_pattern, message.content.lower())
    if rps_match:
        user_choice = rps_match.group(1)
        bot_choice = random.choice(["rock", "paper", "scissors"])

        result = ""
        if user_choice == bot_choice:
            result = f"wah asu bangsat We both picked {user_choice}. (tie)"
        elif (user_choice == "rock" and bot_choice == "scissors") or (
                user_choice == "paper" and bot_choice == "rock") or (
                user_choice == "scissors" and bot_choice == "paper"):
            result = f"fuck u asshole kamu pasti curang literally how did You pick {user_choice}, while i picked {bot_choice}. fuck u (Win)"
        else:
            result = f"LOSERRRRRRRRRRRRRRRRR I picked {bot_choice}, and you picked {user_choice}. (lose)"

        await message_queue.put((message, result))
        return

    compatibility_pattern = r"affakah saya cocok dengan (.+)"
    compatibility_match = re.match(compatibility_pattern, message.content.lower())
    if compatibility_match:
        name = compatibility_match.group(1)
        responses = [
            ":grimacing:",
            f"wait you??? with {name}????",
            "woah uh sure it could work maybe probably....",
            f"yikes kamu dapet ide dari mana mau sama sih {name} bro",
            f"yakin kah?? aku denger {name} kemarin jualan fent di rumah nya luna",
            "sure!!!! like peanut butter and jelly :yum:",
            f"wait u and {name} weren't dating already?",
            f"hohohhohoho you and {name} hol up bro let me get some popcorn first",
            f"welahdalah wes nggak nggak nggak",
            "LMAOOOOOOOOOOOOOOOOOOOOOOOOOOO",
            f"pfft you and {name}? oh wait fr? wowzers.",
            f"i mean... go off, i guess?? {name} tho??",
            "full of drama but okay sure man",
            "wow sounds like a fanfic waiting to happen.",
            f"oh sure, and next you're gonna tell me the sky is green. {name}? lol",
            f"bold of you to assume {name} feels the same way.",
            "hmmmmmm lemme think....................naaaaah.",
            "big moves big moves, but like, sure.",
            "idk bro, it’s giving ‘friends only’ vibes.",
            f"jadi begini, {name} lagi sibuk main minecraft ama aku tadi sih.",
            "100% compatibility! oh wait, salah baca... itu 10%.",
        ]
        response = random.choice(responses)
        await message_queue.put((message, response))
        return

    think_pattern = r"what do you think of (.+) and (.+)"
    think_match = re.match(think_pattern, message.content.lower())
    if think_match:
        person_a, person_b = think_match.groups()
        responses = [
            f"{person_a} and {person_b}????????? {person_a.upper()} AND {person_b.upper()}????????????????????? :cold_sweat:",
            ":sob: :sob: :sob:",
            f"damn bro i mean i heard {person_b} is a saint but with...{person_a}? hmmm....",
            f"well i don't think oil and water can mix well. wait, oh you mean {person_a} and {person_b}?? same thing lah.",
            f"Yes???? obvi???? are u crazy {person_a} and {person_b} basically inseparable are u insane.",
            f"bukane mereka berdua barusan nikahan kemarin? oh blum? huh...",
            f"bro.....i saw {person_a} playing love and deepspace behind {person_b}'s back....",
            f"cocok jir maksude apa kamu tanya kek gitu seng gena.",
            "hoho itu panas banget, sure bro.",
            f"{person_a} and {person_b}? honestly, feels like when you accidentally add too much chili sauce—chaotic but oddly satisfying.",
            f"aku denger mereka barusan duet karaoke lagu sedih, trus {person_a} nangis di pundaknya {person_b}...",
        ]
        response = random.choice(responses)
        await message_queue.put((message, response))
        return

    special_names = []
    if any(name in message.content.lower() for name in special_names):
        await message_queue.put((message, "yayayayaya saya setuju"))
        return

    if "metal kodok" in message.content.lower():
        responses = [
            "halo",
            "yes babe?",
            "sapa manggil woi",
            "berisik ae",
            "^^",
            "lek suka bilang aja twin lolol",
            "yoi",
            "huha",
            "greetings",
            "yo",
            "whats good",
            "u suck balls",
            "im trying to sleep here man",
            "i was playing Mobile Legends: Bang Bang (use code MetalKodok25 to get 25 gems",
            "oh hi kamu kok ganteng hari ini damn",
            "oh hi kamu kok jelek hari ini",
            "fuck you",
            "lho ya ndamau",
            "ak setuju banget",
            "hih",
            "ngeri",
            "gk lucu",
            "kamu mirip logan paul",
            "suruh sorin aja",
            "sek ta lah",
        ]
        response = random.choice(responses)
        await message_queue.put((message, response))
        return

    await bot.process_commands(message)


bot.run(os.getenv("DISCORD_TOKEN"))