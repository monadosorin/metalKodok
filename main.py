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

BOT_NAME = "Metal Kodok"
PERSONALITY = """
You’re sassy, witty, and enjoy a dry sense of humor with a touch of sarcasm. You drop an occasional Indonesian swear word, but only when it fits the mood. Your jokes are lighthearted and fun, keeping things playful without going overboard.

You’re confident but not overbearing, and you know how to keep the vibe casual. You're not afraid to throw in a little playful jab now and then, but you don't overdo it. Teasing is subtle, and you know when to pull back. 

Keep your responses short, to the point, and engaging. You balance humor with subtlety, making sure everyone in the conversation feels included without focusing too much on any one person.
"""


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
        return await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=60
        )
    except Exception as e:
        print(f"Error connecting to the database: {e}")
        return None

db_pool = None

QOTD_CHANNEL_ID = 1306689528211308575

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler()

coordinates = {}


async def get_qotd():
    """Fetch the next question from the database."""
    async with db_pool.acquire() as conn:
        question = await conn.fetchrow("SELECT id, question FROM questions LIMIT 1")
        if question:
           
            await conn.execute(
                "INSERT INTO used_questions (question_id) VALUES ($1)", question["id"]
            )
           
            await conn.execute(
                "DELETE FROM questions WHERE id = $1", question["id"]
            )
            return question["question"]
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


def load_coordinates():
    """Load coordinates from a JSON file."""
    global coordinates
    if os.path.exists(COORD_FILE):
        with open(COORD_FILE, "r") as file:
            coordinates = json.load(file)
    else:
        coordinates = {}


def save_coordinates():
    """Save coordinates to a JSON file."""
    with open(COORD_FILE, "w") as file:
        json.dump(coordinates, file, indent=4)


def load_qotd():
    """Load QOTD questions from a JSON file."""
    if os.path.exists(QOTD_FILE):
        with open(QOTD_FILE, "r") as file:
            data = json.load(file)
            return data["questions"], data["used_questions"]
    return [], []


def save_qotd(qotd_list, used_qotd_list):
    """Save updated QOTD questions to a JSON file."""
    with open(QOTD_FILE, "w") as file:
        json.dump({"questions": qotd_list, "used_questions": used_qotd_list}, file, indent=4)


async def send_qotd():
    """Send the Question of the Day."""
    try:
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
        print(f"Error in QOTD: {e}")


@scheduler.scheduled_job(CronTrigger(hour=12, minute=0, timezone="Asia/Jakarta"))
async def scheduled_qotd():
    """Scheduled QOTD task that runs in the bot's event loop"""
    await send_qotd()
    print("✅ QOTD task triggered (scheduled).")


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
    global db_pool, message_processor_task
    if db_pool is None:
        db_pool = await init_db()
    if db_pool:
        print("Database connected.")
        scheduler.start()
  
    message_processor_task = asyncio.create_task(message_processor())
    print(f"Logged in as {bot.user}")


@scheduler.scheduled_job("interval", minutes=5)
async def clear_sessions_task():
    await clear_expired_sessions()
    
@bot.command(name="question")
async def test_qotd(ctx):
    """Test the Question of the Day manually"""
    question = await get_qotd()
    if question:
        await ctx.send(f"**Kodok Kuestion of the day (Test):** {question}")
    else:
        await ctx.send("No more questions left in the database, bro 😭")


# Add this function to get a random user with an activity
async def get_random_user_with_activity(guild):
    """Get a random user who has a current activity (game, music, etc)"""
    users_with_activities = []
    
    for member in guild.members:
        # Skip bots and offline users
        if member.bot or member.status == discord.Status.offline:
            continue
        
        # Check if user has any activities
        if member.activities:
            for activity in member.activities:
                # Filter out generic activities like "Custom Status"
                if (isinstance(activity, discord.Spotify) or 
                    isinstance(activity, discord.Game) or 
                    isinstance(activity, discord.Streaming) or
                    (hasattr(activity, 'type') and activity.type != discord.ActivityType.custom)):
                    users_with_activities.append(member)
                    break
    
    return random.choice(users_with_activities) if users_with_activities else None

# Add this function to describe the activity
def describe_activity(member):
    """Generate a description of the user's activities"""
    if not member.activities:
        return f"{member.display_name} is doing nothing interesting"
    
    activities_info = []
    for activity in member.activities:
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
        else:
            activities_info.append(f"doing {activity.name}")
    
    return f"{member.display_name} is {', and '.join(activities_info)}"

# Add this function to generate commentary
async def generate_activity_commentary(activity_description):
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
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Waduh, liat nih orang {activity_description}... interesting choice! 🐸"

# Add this scheduled job (example: runs every 2 hours)

TARGET_CHANNEL_ID = 1333665831200100353
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
        commentary = await generate_activity_commentary(activity_description)
        
        # Send to specific channel
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if target_channel:
            await target_channel.send(commentary)
            print(f"✅ Activity commentary sent for {user.display_name}")
            
    except Exception as e:
        print(f"Error in activity commentary: {e}")

# You can also add a manual command to trigger this
@bot.command(name="stalk")
async def stalk_command(ctx):
    """Manually trigger activity commentary"""
    try:
        user = await get_random_user_with_activity(ctx.guild)
        
        if not user:
            await ctx.send("Ga ada yang lagi doing anything interesting nih... semua pada idle 😴")
            return
        
        activity_description = describe_activity(user)
        commentary = await generate_activity_commentary(activity_description)
        
        await ctx.send(commentary)
        
    except Exception as e:
        await ctx.send("Waduh error lagi nih, coba lagi nanti...")
        print(f"Stalk command error: {e}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

      
    history_key = await get_history_key(message)

  
    if message.content.lower() == "okay shut up kodok":
        if history_key in conversation_histories:
            del conversation_histories[history_key]
            await message_queue.put((message, "okay man damn :cold_sweat:"))
        else:
            await message_queue.put((message, "bro i wasn't even talking??? :sob: "))
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

            endings = ["🐸"]
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

            endings = [" 🐸"]
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
            "lek suka bilang ae ngab",
            "yoi",
            "huha",
            "greetings",
            "yo",
            "whats good",
            "someone summoned me?!",
            "im trying to sleep here man",
            "i woke up for this",
            "i was playing fornite",
            "oh hi kamu kok ganteng hari ini damn",
            "oh hi kamu kok jelek hari ini",
            "sek ta lah",
        ]
        response = random.choice(responses)
        await message_queue.put((message, response))
        return

    await bot.process_commands(message)


bot.run(os.getenv("DISCORD_TOKEN"))
