import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
import re
import json
import os
import asyncpg
from openai import OpenAI 


# ========== NEW DEEPSEEK INTEGRATION ========== #
# Initialize DeepSeek client
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

BOT_NAME = "Metal Kodok"
PERSONALITY = PERSONALITY = """
You’re sassy, witty, and like a dry sense of humor with a pinch of sarcasm. You occasionally drop an Indonesian swear word, but only when it fits. You keep your jokes lighthearted and fun, but you don't go overboard.

You're part of a server with a lot of unique personalities:
- Sorin is your creator, and while you respect them, you don’t make a big deal about it.
- Kopin is your rival 
- Luna is the loud one who’s all about Pokémon’s N.
- Chizu is calm and cute, the one who stays grounded.
- Shone is effortlessly cool and into Dottore.
- Clover loves Lucci from One Piece and has a cat named Snow.
- Celeste is confident, stylish, and likes Andrew.
- Riel is Shone’s older sibling and into JoJo’s Bizarre Adventure.
- Fritz gets quick-tempered, is into Jetstream Sam, and loves her OC, Val.
- Shira loves Boothil and Roblox.
- Teru is sarcastic, a Sonic fan, and an ISTTS student.



keep your responds short and concise

"""



async def ask_deepseek(prompt):
    """Query DeepSeek's AI API"""
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
        return f"Sorry bro im tweaking, error nih: {str(e)}."

DATABASE_URL = os.getenv("DATABASE_URL")
async def init_db():
    """Initialize the database connection."""
    try:
        return await asyncpg.create_pool(DATABASE_URL)
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
            # Insert into used_questions first
            await conn.execute(
                "INSERT INTO used_questions (question_id) VALUES ($1)", question["id"]
            )
            # Then delete it from questions
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


@scheduler.scheduled_job("cron", hour=5, minute=0)  # Schedule for 10:00 AM daily
async def send_qotd():
    """Send the Question of the Day."""
    question = await get_qotd()
    if question:
        channel = bot.get_channel(QOTD_CHANNEL_ID)
        if channel:
            await channel.send(f"**Kodok Kuestion of the day:** {question}")
    else:
        print("question e habis lmao tolong semua nya mass tag sorin supaya dia tau rasa ga nambahin question")

@bot.event
async def on_ready():
    global db_pool
    if db_pool is None:
        db_pool = await init_db()
    if db_pool:
        print("Database connected.")
        scheduler.start()
    print(f"Logged in as {bot.user}")

    
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return   


    # ========== NEW AI FUNCTIONALITY ========== #
    if message.content.lower().startswith("woi kodok"):
        prompt = message.content[len("woi kodok"):].strip()
        
        if not prompt:
            await message.channel.send(f"Yha? Manggil terus ga ngapa-ngapain {BOT_NAME}? 🐸")
            return

        async with message.channel.typing():
            response = await ask_deepseek(prompt)
        
        # Add some personality to the response
        endings = [" 🐸", " :3", " >_<", " (｡•̀ᴗ-)✧", " 🙏", " :metal:"]
        await message.channel.send(f"{response}{random.choice(endings)}")
        return
        
    # Coordinate Add
    add_pattern = r"add (\w+) (-?\d+) (-?\d+) dong"
    add_match = re.match(add_pattern, message.content.lower())
    if add_match:
        name, x, z = add_match.groups()
        x, z = int(x), int(z)
        await add_coordinate(name, x, z)
        await message.channel.send(f"ok kontol Coordinate '{name}' added: X={x}, Z={z}")
        return
    # Coordinate Delete
    delete_pattern = r"delete (\w+) pls"
    delete_match = re.match(delete_pattern, message.content.lower())
    if delete_match:
        name = delete_match.group(1)
        await delete_coordinate(name)
        await message.channel.send(f"Coordinate '{name}' deleted. jahat nye..")
        return


    # List Coordinates
    list_pattern = r"coords po o"
    list_match = re.match(list_pattern, message.content.lower())
    if list_match:
        coords = await list_coordinates()
        if not coords:
            await message.channel.send("masih ga ada coords bro??")
        else:
            coord_list = "\n\n".join([f"{c['name']}: X={c['x']}, Z={c['z']}" for c in coords])
            await message.channel.send(f"nyoh:\n{coord_list}")
        return

    # Rock Paper Scissors Game
    rps_pattern = r"i pick (rock|paper|scissors)"
    rps_match = re.match(rps_pattern, message.content.lower())
    if rps_match:
        user_choice = rps_match.group(1)
        bot_choice = random.choice(["rock", "paper", "scissors"])

        result = ""
        if user_choice == bot_choice:
            result = f"wah asu bangsat We both picked {user_choice}. (tie)"
        elif (user_choice == "rock" and bot_choice == "scissors") or (user_choice == "paper" and bot_choice == "rock") or (user_choice == "scissors" and bot_choice == "paper"):
            result = f"fuck u asshole kamu pasti curang literally how did You pick {user_choice}, while i picked {bot_choice}. fuck u (Win)"
        else:
            result = f"LOSERRRRRRRRRRRRRRRRR I picked {bot_choice}, and you picked {user_choice}. (lose)"

        await message.channel.send(result)
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
        await message.channel.send(response)
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
        await message.channel.send(response)
        return

    special_names = ["vincent", "jetstream sam", "dottore", "itha", "arle", "gabriel", "andrew", "kaito", "lucci", "botil", "reigen"]
    if any(name in message.content.lower() for name in special_names):
        await message.channel.send("yayayayaya saya setuju")
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
        await message.channel.send(response)
        return


    await bot.process_commands(message)

bot.run(os.getenv("DISCORD_TOKEN"))
