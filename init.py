import math
import sqlite3
import time
import yaml
import discord
import re
from quickchart import QuickChart

config = yaml.safe_load(open("config.yaml"))
connection = sqlite3.connect("db/database.db")
client = discord.Client()

coin1 = {
    "name": "SBCoin",
    "emoji": "<:sbcoin:1032063250478661672>",
    "emoji_name": "sbcoin",
    "user": "SBCoin#6868",
    "balance": 0,
    "transaction_fee": lambda x: math.ceil(x * 0.02),
    "get_transfer_amount": lambda m: int(re.search(r"sent (\d+) SBCoin to <@1343666037551267904>", m).group(1)),
    "command_id": 1323310639623700551,
    "send": lambda c, m, a, u: c.__call__(channel=m.channel, recipient=u, amount=a)
}
coin2 = {
    "name": "DABCoin",
    "emoji": "<:dabcoin:1342578198269137019>",
    "emoji_name": "dabcoin",
    "user": "DABCoin#1056",
    "balance": 0,
    "transaction_fee": lambda _: 0,
    "get_transfer_amount": lambda m: int(re.search(r"transferred (\d+) [Dd][aA][bB][cC]oins? to <@1343666037551267904>", m).group(1)),
    "command_id": 1342574310593921027,
    "send": lambda c, m, a, u: c.__call__(channel=m.channel, user=u, amount=a)
}
coins = [coin1, coin2]

suppliers = {}
commands = {}

last_price_update = 0
update_time = 60 * 60

supplier_timeout = 30
supply_fee = 0.05

# Update balances from database
coin1["balance"] = connection.execute("SELECT balance FROM coins WHERE name = ?", [coin1["name"]]).fetchone()[0]
coin2["balance"] = connection.execute("SELECT balance FROM coins WHERE name = ?", [coin2["name"]]).fetchone()[0]
connection.commit()

def update_price_history():
    global last_price_update
    if time.time() - last_price_update > update_time:
        last_price_update = time.time()

        last_update_in_db = connection.execute("SELECT time FROM history ORDER BY time DESC LIMIT 1").fetchone()
        if last_update_in_db and time.time() - last_update_in_db[0] < update_time:
            return

        price1 = get_conversion(1, coin1, coin2, with_transaction_fee=False, with_rounding=False)[0]
        price2 = get_conversion(1, coin2, coin1, with_transaction_fee=False, with_rounding=False)[0]

        connection.execute("INSERT INTO history (time, coin_name, price, supply) VALUES (?, ?, ?, ?)", (last_price_update, coin1["name"], price1, coin1["balance"]))
        connection.execute("INSERT INTO history (time, coin_name, price, supply) VALUES (?, ?, ?, ?)", (last_price_update, coin2["name"], price2, coin2["balance"]))
        connection.commit()

def add_to_balance(coin, amount):
    coin["balance"] += amount
    connection.execute("UPDATE coins SET balance = ? WHERE name = ?", (coin["balance"], coin["name"]))
    connection.commit()

    update_price_history()

def get_conversion(transfer_amount: int, coin1, coin2, with_transaction_fee=True, with_rounding=True):
    if transfer_amount < 0:
        return 0

    k = coin1["balance"] * coin2["balance"]
    new_coin1_balance = coin1["balance"] + transfer_amount
    raw_coins_given = coin2["balance"] - (k / float(new_coin1_balance))
    coins_given = raw_coins_given

    total_supply_fee = coins_given * supply_fee
    coins_given -= total_supply_fee

    if with_rounding:
        coins_given = math.floor(coins_given)
    
    total_given_to_suppliers = raw_coins_given - coins_given

    if with_transaction_fee:
        coins_given -= coin2["transaction_fee"](coins_given)

    return coins_given, total_given_to_suppliers

async def create_message(message, coin1, coin2, coins_given, total_given_to_suppliers: int):
    transfer_amount = coin1["get_transfer_amount"](message.content)
    fees = coin1["transaction_fee"](coins_given)
    content = f"{transfer_amount} {coin1['emoji']} {coin1['name']} converted to {coins_given} {coin2['emoji']} {coin2['name']}"

    if coins_given > 0 and coin2["balance"] >= coins_given:
        add_to_balance(coin1, transfer_amount)
        add_to_balance(coin2, - (coins_given + fees))

        content += f"\n{"{:.6f}".format(total_given_to_suppliers)} {coin2['emoji']} {coin2['name']} fee given to our generous suppliers"
        await message.reply(content=content)
        await send(message, message.interaction.user, coin2, coins_given)
        
        distribute_supply_fees(coin2, total_given_to_suppliers)
    elif coins_given > 0:
        # Not enough money to give out
        content = f"\nNot enough supply of {coin2['emoji']} {coin2['name']} in the swapping pool to give out {coins_given} {coin2['emoji']} {coin2['name']}"
        await message.reply(content=content)
    else:
        # Sent too little to convert
        if transfer_amount - fees <= 0:
            content += f"\nNot sending your {coin1['emoji']} {coin1['name']} back due to {fees} {coin2['emoji']} {coin2['name']} transaction fee"
            await message.reply(content=content)
        else:
            content += f"\nSending your {coin1['emoji']} {coin1['name']} back{f" with {fees} {coin2['emoji']} {coin2['name']} transaction fee" if fees else ""}"
            await message.reply(content=content)

            await send(message, message.interaction.user, coin1, transfer_amount - fees)

async def add_supply(message, coin, transfer_amount: int):
    add_to_balance(coin, transfer_amount)

    connection.execute("""
        INSERT INTO suppliers (userID, amount, coin_name)
        VALUES (?, ?, ?)
        ON CONFLICT(userID, coin_name)
        DO UPDATE SET amount = amount + excluded.amount;
    """, (message.interaction.user.id, transfer_amount, coin["name"]))
    connection.commit()

    await message.reply(content=f"Added {transfer_amount} {coin['emoji']} {coin['name']} to the supply. You will receive a {supply_fee * 100}% fee for each trade")

def distribute_supply_fees(coin, total_supply_fee: int):
    total_supply = connection.execute("SELECT SUM(amount) FROM suppliers WHERE coin_name = ?", [coin["name"]]).fetchone()[0]

    for supplier in connection.execute("SELECT userID, amount FROM suppliers WHERE coin_name = ?", [coin["name"]]).fetchall():
        user_id, amount = supplier
        gained_fee = amount / float(total_supply) * total_supply_fee

        connection.execute("UPDATE suppliers SET fees_collected = fees_collected + ? WHERE userID = ? AND coin_name = ?", (gained_fee, user_id, coin["name"]))

    connection.commit()

async def send(message, user, coin, amount: int):
    command = [c for c in commands[message.channel.id] if c.id == coin["command_id"]][0]
    if command:
        await coin["send"](command, message, amount, user)

async def handle_conversion(message, coin1, coin2):
    try:
        transfer_amount = coin1["get_transfer_amount"](message.content)

        if message.interaction.user.id in suppliers and time.time() - suppliers[message.interaction.user.id] < supplier_timeout:
            await add_supply(message, coin1, transfer_amount)
            return

        coins_given, total_given_to_suppliers = get_conversion(transfer_amount, coin1, coin2)
        await create_message(message, coin1, coin2, coins_given, total_given_to_suppliers)

        print(f"{coin1['name']} balance: {coin1['balance']}, {coin2['name']} balance: {coin2['balance']}")
    except AttributeError:
        # Not a valid transfer message
        return
    
def get_emoji(coin_name):
    for coin in coins:
        if coin["name"] == coin_name:
            return coin["emoji"]

def get_supply(coin_name):
    for coin in coins:
        if coin["name"] == coin_name:
            return coin["balance"]
        
def make_chart():
    data = connection.execute("SELECT time, price, supply FROM history WHERE coin_name = ? ORDER BY time", [coin1["name"]]).fetchall()
    data2 = connection.execute("SELECT supply FROM history WHERE coin_name = ? ORDER BY time", [coin2["name"]]).fetchall()

    chart_precision = update_time

    data.append((time.time(), get_conversion(1, coin1, coin2, with_transaction_fee=False, with_rounding=False)[0], coin1["balance"]))
    data2.append((coin2["balance"],))

    processed_data = []
    index = 0
    for t, price, supply in data:
        current_data_point = {
            "time": t,
            "price": price,
            "dab_supply": data2[index][0],
            "sb_supply": supply
        }

        if len(processed_data) > 0:
            while current_data_point["time"] - processed_data[-1]["time"] > chart_precision:
                processed_data.append({
                    "time": processed_data[-1]["time"] + chart_precision,
                    "price": processed_data[-1]["price"],
                    "dab_supply": processed_data[-1]["dab_supply"],
                    "sb_supply": processed_data[-1]["sb_supply"]
                })
            
            processed_data.append({
                "time": processed_data[-1]["time"] + chart_precision,
                "price": current_data_point["price"],
                "dab_supply": current_data_point["dab_supply"],
                "sb_supply": current_data_point["sb_supply"]
            })
        else:
            processed_data.append(current_data_point)

        index += 1

    chart1 = QuickChart()
    chart1.width = 500
    chart1.height = 300
    chart1.device_pixel_ratio = 2.0
    chart1.background_color = "black"
    chart1.config = {
        "type": "line",
        "data": {
            "labels": [time.strftime("%m-%d", time.localtime(d["time"])) for d in processed_data],
            "datasets": [{
                "label": "SBCoin to DABCoin",
                "borderColor": "#fecd4c",
                "fill": False,
                "data": [d["price"] for d in processed_data],
            }]
        },
        "options": {
            "legend": {
                "labels": {
                    "fontColor": "white"
                }
            },
            "scales": {
                "yAxes": [{
                    "ticks": {
                        "fontColor": "white",
                        "beginAtZero": True
                    },
                    "gridLines": {
                        "display": True,
                        "color": "#383838",
                        "zeroLineColor": "#383838",
                        "lineWidth": 2
                    }
                }],
                "xAxes": [{
                    "ticks": {
                        "fontColor": "white"
                    }
                }]
            }
        }
    }

    chart2 = QuickChart()
    chart2.width = 500
    chart2.height = 300
    chart2.device_pixel_ratio = 2.0
    chart2.background_color = "black"
    chart2.config = {
        "type": "line",
        "data": {
            "labels": [time.strftime("%m-%d", time.localtime(d["time"])) for d in processed_data],
            "datasets": [{
                "label": "DABCoin supply",
                "borderColor": "#3b3db0",
                "fill": False,
                "data": [d["dab_supply"] for d in processed_data],
            },{
                "label": "SBCoin supply",
                "borderColor": "#ff0000",
                "fill": False,
                "data": [d["sb_supply"] for d in processed_data],
            }]
        },
        "options": {
            "legend": {
                "labels": {
                    "fontColor": "white"
                }
            },
            "scales": {
                "yAxes": [{
                    "ticks": {
                        "fontColor": "white",
                        "beginAtZero": True
                    },
                    "gridLines": {
                        "display": True,
                        "color": "#383838",
                        "zeroLineColor": "#383838"
                    }
                }],
                "xAxes": [{
                    "ticks": {
                        "fontColor": "white"
                    }
                }]
            }
        }
    }

    return chart1.get_short_url(), chart2.get_short_url()

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')

async def update_commands(message):
    global commands
    if message.channel.id not in commands:
        commands[message.channel.id] = await message.channel.application_commands()
    
@client.event
async def on_message(message):
    if str(message.author) == coin1["user"]:
        # SBCoin to DABCoin
        await update_commands(message)
        await handle_conversion(message, coin1, coin2)
    elif str(message.author) == coin2["user"]:
        # DABCoin to SBCoin
        await update_commands(message)
        await handle_conversion(message, coin2, coin1)
    elif message.content.startswith("!hodl"):
        price1 = get_conversion(1, coin1, coin2, with_transaction_fee=False, with_rounding=False)[0]
        price2 = get_conversion(1, coin2, coin1, with_transaction_fee=False, with_rounding=False)[0]
        url1, url2 = make_chart()

        await message.reply(content=f"1 {coin1['emoji']} {coin1['name']} is worth {"{:.4f}".format(price1)} {coin2['emoji']} {coin2['name']}\n1 {coin2['emoji']} {coin2['name']} is worth {"{:.4f}".format(price2)} {coin1['emoji']} {coin1['name']}\n\nThere are {coin1['balance']} {coin1['emoji']} {coin1['name']} and {coin2['balance']} {coin2['emoji']} {coin2['name']} in the swapping pool\n-# [1]({url1}) [2]({url2})")
    elif message.content.startswith("!supply"):
        await message.reply(content=f"Anything you send in the next 30 seconds will be added to the supply. You will earn part of the {supply_fee * 100}% fee for each trade")

        suppliers[message.author.id] = time.time()
    elif message.content.startswith("!balance"):
        supply_info = connection.execute("SELECT coin_name, amount, fees_collected FROM suppliers WHERE userID = ?", [message.author.id]).fetchall()

        if len(supply_info) > 0:
            supply_info = "\n".join([f"{get_emoji(coin_name)} {coin_name}: {amount} collecting fees worth {"{:.6f}".format(fees_collected)} {get_emoji(coin_name)} {coin_name}" for coin_name, amount, fees_collected in supply_info])
            await message.reply(content=f"Your supply:\n{supply_info}")
        else:   
            await message.reply(content="You aren't a grifter yet")
    elif message.content.startswith("!suppliers"):
        supply_info = connection.execute("SELECT coin_name, amount, fees_collected, userID FROM suppliers order by coin_name, amount desc").fetchall()

        content = ""
        for (coin_name, amount, fees_collected, user_id) in supply_info:
            if amount > 0 or fees_collected > 0:
                content += f"<@{user_id}>: {coin_name} {amount} with fees of {"{:.6f}".format(fees_collected)}({"{:.2f}".format((amount / get_supply(coin_name)) * 100)}%)\n"

        await message.reply(content=content, allowed_mentions=discord.AllowedMentions.none())

    elif message.content.startswith("!withdraw"):
        args = message.content.split(" ")
        if len(args) != 3:
            await message.reply(content="Must specify the coin and amount to withdraw")
            return

        if args[1].lower() == coin1["name"].lower():
            coin = coin1
        elif args[1].lower() == coin2["name"].lower():
            coin = coin2
        else:
            await message.reply(content="Invalid coin")
            return
        
        withdraw_ask_amount_string = args[2]
        if withdraw_ask_amount_string.isdigit():
            withdraw_amount = int(withdraw_ask_amount_string)
            transaction_fee = coin["transaction_fee"](withdraw_ask_amount_string)
            withdraw_amount = withdraw_ask_amount_string + transaction_fee
            if withdraw_amount <= 0:
                await message.reply(content="Amount must be greater than 0")
                return
        elif withdraw_ask_amount_string.lower() == "all":
            transaction_fee = coin["transaction_fee"](coin["balance"])
            withdraw_amount = coin["balance"] + transaction_fee
        else:
            await message.reply(content="Invalid amount")
            return
        
        amount, fees_collected = connection.execute("SELECT SUM(amount + fees_collected) as amount, fees_collected FROM suppliers WHERE userID = ? AND coin_name = ?", [message.author.id, coin["name"]]).fetchone()
        if amount is None:
            await message.reply(content=f"You don't have any {coin['emoji']} {coin['name']} in the supply")
            return
        elif amount < withdraw_amount:
            await message.reply(content=f"You don't have enough {coin['emoji']} {coin['name']} in the supply")
            return
        else:
            if fees_collected >= withdraw_amount:
                # Only take from fees collected
                connection.execute("UPDATE suppliers SET fees_collected = fees_collected - ? WHERE userID = ? AND coin_name = ?", (withdraw_amount, message.author.id, coin["name"]))
                connection.commit()
            else:
                fractional_fees = fees_collected - math.floor(fees_collected)
                connection.execute("UPDATE suppliers SET amount = amount - ?, fees_collected = ? WHERE userID = ? AND coin_name = ?", (withdraw_amount - math.floor(fees_collected), fractional_fees, message.author.id, coin["name"]))
                connection.commit()

            add_to_balance(coin, -withdraw_amount)
            await message.reply(content=f"Withdrew {withdraw_amount} {coin['emoji']} {coin['name']} from the supply{f' with {transaction_fee} {coin["emoji"]} {coin["name"]} transaction fee' if transaction_fee else ''}")
            
            await send(message, message.author, coin, withdraw_ask_amount_string)
    elif message.content.startswith("!forget"):
        args = message.content.split(" ")
        if len(args) > 2:
            await message.reply(content="Too many arguments")
            return
        coin_1_balance = coin1["balance"]
        coin_1_balance_rounded = math.ceil(coin_1_balance)
        coin_2_balance = coin2["balance"]
        coin_2_balance_rounded = math.ceil(coin_2_balance)
        
        force = args[1] == "force"
        if (coin_1_balance_rounded == 0 and coin_2_balance_rounded == 0) or force:
            # TODO Spread out the user's remaining coin fractions to the remaining suppliers
            connection.execute("DELETE FROM suppliers WHERE userID = ?", [message.author.id])
            connection.commit()
            await message.reply(content="You have been forgotten")
        else:
            await message.reply(content="You must have less than 1 balance in both coins to be forgotten")


@client.event
async def on_raw_reaction_add(reaction):
    if reaction.message_author_id == client.user.id:
        if reaction.emoji.name == coin1["emoji_name"]:
            add_to_balance(coin1, 1)
        elif reaction.emoji.name == coin2["emoji_name"]:
            add_to_balance(coin2, 1)

client.run(config["token"])