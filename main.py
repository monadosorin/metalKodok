import discord
from discord.ext import commands
import random
import re
import json
import os

COORD_FILE = "coordinates.json"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

coordinates = {}

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

@bot.event
async def on_ready():
    load_coordinates()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return


   
        
    # Coordinate Add
    add_pattern = r"add (\w+) (-?\d+) (-?\d+) dong"
    add_match = re.match(add_pattern, message.content.lower())
    if add_match:
        name, x, z = add_match.groups()
        x, z = int(x), int(z)
        coordinates[name] = {"x": x, "z": z}
        save_coordinates()
        await message.channel.send(f"ok kontol Coordinate '{name}' added: X={x}, Z={z}")
        return

    # Coordinate Delete
    delete_pattern = r"delete (\w+) pls"
    delete_match = re.match(delete_pattern, message.content.lower())
    if delete_match:
        name = delete_match.group(1)
        if name in coordinates:
            del coordinates[name]
            save_coordinates()
            await message.channel.send(f"Coordinate '{name}' deleted. jahat nye..")
        else:
            await message.channel.send(f"mana ada yang nama nya'{name}'")
        return

    # List Coordinates
    list_pattern = r"coords po o"
    list_match = re.match(list_pattern, message.content.lower())
    if list_match:
        if not coordinates:
            await message.channel.send("masih ga ada coords bro??")
        else:
            coord_list = "\n\n".join([f"{name}: X={data['x']}, Z={data['z']}" for name, data in coordinates.items()])
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
            "LMAOOOOOOOOOOOOOOOOOOOOOOOOOOO",
        ]
        response = random.choice(responses)
        await message.channel.send(response)
        return

    special_names = ["vincent", "sam", "dottore", "itha", "arle", "gabriel", "andrew", "kaito", "lucci", "botil", "reigen"]
    if any(name in message.content.lower() for name in special_names):
        await message.channel.send("yayayayaya saya setuju")
        return


    if "metal kodok" in message.content.lower():
        await message.channel.send("halo")
        return


    await bot.process_commands(message)

bot.run(os.getenv("DISCORD_TOKEN"))
