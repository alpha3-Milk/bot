# bot.py
import os
import random
import json
import asyncio
import logging
from typing import Optional, Set, List, Tuple

import discord
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("minigames-bot")

# ---------- persistence ----------
DATA_FILE = "player_money.json"
SAVE_LOCK = asyncio.Lock()

def load_money() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.exception("player_money.json 読み込み失敗: %s", e)
        return {}

async def save_money_async(data: dict):
    async with SAVE_LOCK:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp, DATA_FILE)

player_money = load_money()

# ---------- bot init ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def ensure_user_init(uid: str, base: int = 10000):
    if uid not in player_money:
        player_money[uid] = base

async def change_money(uid: str, delta: int):
    ensure_user_init(uid)
    player_money[uid] = player_money.get(uid, 0) + delta
    await save_money_async(player_money)

# =========================
# PlayAgainView (global, holds game_type)
# =========================
class PlayAgainView(View):
    def __init__(self, ctx, game_type: str, *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.game_type = game_type
        self.user = ctx.author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("これはあなたのボタンではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="もう一度プレイ", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: Button):
        # Ensure only the original player can use
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("このボタンはあなたのものではありません。", ephemeral=True)

        # Respond ephemerally to acknowledge
        await interaction.response.send_message(f"{interaction.user.mention} 再プレイを開始します…", ephemeral=True)

        # Invoke proper starter
        if self.game_type == "poker":
            await start_poker(self.ctx)
        elif self.game_type == "horse":
            await start_horse_race(self.ctx)
        else:
            await interaction.followup.send("不明なゲームタイプです。", ephemeral=True)

# ========== Shared BetView (競馬とポーカー共通) ==========
class BetView(View):
    def __init__(self, actor_id: int, *, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.actor_id = actor_id
        self.value: Optional[int] = None

        # 固定額
        for amount in [100, 500, 1000, 5000, 10000, 100000]:
            self.add_item(BetButton(str(amount), amount))
        self.add_item(ManualBetButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("これはあなたの操作ではありません。", ephemeral=True)
            return False
        return True

class BetButton(Button):
    def __init__(self, label: str, amount: int):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.amount = amount

    async def callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        ensure_user_init(uid)
        bal = player_money.get(uid, 0)
        if self.amount > bal:
            await interaction.response.send_message(f"{interaction.user.mention} ⚠ 所持金が足りません（{bal}）", ephemeral=True)
            return
        view: BetView = self.view  # type: ignore
        view.value = self.amount
        # safe reply (first response)
        await interaction.response.send_message(f"{interaction.user.mention} 賭け金を **{self.amount}** に設定しました。", ephemeral=True)
        view.stop()

class ManualBetButton(Button):
    def __init__(self):
        super().__init__(label="手入力", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        actor = interaction.user
        uid = str(actor.id)
        ensure_user_init(uid)
        bal = player_money.get(uid, 0)

        await interaction.response.send_message(f"{actor.mention} 手入力モード：チャットで金額を送ってください。（整数）", ephemeral=True)

        def check(m: discord.Message):
            return m.author.id == actor.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=30)
            amt = int(msg.content.strip())
        except asyncio.TimeoutError:
            return await interaction.followup.send(f"{actor.mention} ⏰ 時間切れです。", ephemeral=True)
        except Exception:
            return await interaction.followup.send(f"{actor.mention} ⚠ 整数で入力してください。", ephemeral=True)

        if amt <= 0 or amt > bal:
            return await interaction.followup.send(f"{actor.mention} ⚠ 所持金の範囲で入力してください（所持: {bal}）", ephemeral=True)

        view: BetView = self.view  # type: ignore
        view.value = amt
        await interaction.followup.send(f"{actor.mention} 賭け金を **{amt}** に設定しました。", ephemeral=True)
        view.stop()

async def ask_bet_via_interaction(interaction: discord.Interaction, actor_id: int) -> Optional[int]:
    """Interaction 内で呼び出す賭け金選択。戻り値は選ばれた金額または None"""
    view = BetView(actor_id)
    embed = discord.Embed(title="💰 賭け金を選択", description=f"<@{actor_id}> 下のボタンから賭け金を選んでください。", color=0xffcc00)
    # defer then followup pattern (ask_bet will own the followup)
    await interaction.response.defer(ephemeral=True)
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=False)
    await view.wait()
    # view.value may be set or None
    if view.value is None:
        await interaction.followup.send(f"<@{actor_id}> 賭け金が選ばれずキャンセルされました。", ephemeral=True)
        return None
    return view.value

async def ask_bet_via_ctx(ctx: commands.Context) -> Optional[int]:
    """ctx ベースで呼ぶ既存関数（残しておく）"""
    view = BetView(ctx.author.id)
    embed = discord.Embed(title="💰 賭け金を選択", description=f"{ctx.author.mention} 下のボタンから賭け金を選んでください。", color=0xffcc00)
    msg = await ctx.send(embed=embed, view=view)
    await view.wait()
    if view.value is None:
        try:
            await msg.edit(content=f"{ctx.author.mention} 賭け金未選択でキャンセルされました。", embed=None, view=None)
        except Exception:
            pass
        return None
    return view.value

# =========================
# 競馬 (horse race)
# =========================
horsename = ['アルファミナミトリ','ハリボテエレジー','ナンヨウフェニックス','アカイスイセイ','リュウセイスター',
    'ユメノイズミ','モルアルテェテェ','オーロラデオキシス','エビトマトマツリ','チンチロゾロメ',
    'ナンヨーリニア','クサノネッコ','ナカノトリリン','デキチャッタ','バクソウチャリオット']
horsesize = {'アルファミナミトリ':9,'ハリボテエレジー':8,'ナンヨウフェニックス':10,'アカイスイセイ':7,
    'リュウセイスター':8,'ユメノイズミ':6,'モルアルテェテェ':8,'オーロラデオキシス':9,'エビトマトマツリ':8,
    'チンチロゾロメ':7,'ナンヨーリニア':7,'クサノネッコ':6,'ナカノトリリン':7,'デキチャッタ':6,'バクソウチャリオット':10}

def calc_horse_rank(horse, othersize: List[int]) -> float:
    size = horsesize[horse]
    if size == 6:
        rank = (sum(othersize))/30 + random.randint(100,150)/100
    elif size == 7:
        rank = (othersize[0]-othersize[1]+othersize[2]+othersize[3])/4
    elif size == 8:
        rank = (othersize[0]/othersize[1] + othersize[2]/othersize[3])/2
        if rank < 1:
            rank = 1 + random.randint(10,100)/100
    elif size == 9:
        rank = (othersize[0]*othersize[1]*othersize[2]/othersize[3])
    elif size == 10:
        rank = (othersize[0]*10 - othersize[1]*10 + othersize[2]*10 - othersize[3]*10) + 100
    return round(rank, 2)

class HorseSelectView(View):
    def __init__(self, ctx, horses: List[str], ranks: List[float]):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.user = ctx.author
        self.horses = horses
        self.ranks = ranks
        # create buttons
        for i in range(len(horses)):
            self.add_item(HorseButton(str(i+1), i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("これはあなたのレースではありません。", ephemeral=True)
            return False
        return True

class HorseButton(Button):
    def __init__(self, label: str, index: int):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        # Called when user picks a horse -> then ask for bet, then resolve
        view: HorseSelectView = self.view  # type: ignore
        idx = self.index
        horse_name = view.horses[idx]
        rate = view.ranks[idx]
        user_id = interaction.user.id

        # Ask bet via interaction (this will defer and send an embed with BetView)
        bet = await ask_bet_via_interaction(interaction, user_id)
        if bet is None:
            return  # user cancelled or timeout

        # Check balance again (ask_bet already checks for amounts <= balance, but ensure)
        uid = str(user_id)
        ensure_user_init(uid)
        bal = player_money.get(uid, 10000)
        if bet > bal:
            await interaction.followup.send(f"{interaction.user.mention} ⚠ 所持金が足りません（{bal}）", ephemeral=True)
            return

        # perform result roll
        roll = random.random()
        win = roll < (1.0 / rate)
        if win:
            payout = round(bet * rate)
            # net effect: subtract stake then add payout
            player_money[uid] = player_money.get(uid, 10000) - bet + payout
            result_text = f"🎉 当たり！ 獲得: {payout} ミナミトリウム"
        else:
            player_money[uid] = player_money.get(uid, 10000) - bet
            result_text = f"❌ はずれ… 賭け金 {bet} を失いました"

        await save_money_async(player_money)

        embed = discord.Embed(title="🏁 レース結果", color=0x00ffcc)
        embed.add_field(name="あなたが選んだ馬", value=f"{idx+1}番：{horse_name}", inline=False)
        embed.add_field(name="倍率", value=str(rate), inline=True)
        embed.add_field(name="勝率", value=f"{round((1/rate)*100, 2)}%", inline=True)
        embed.add_field(name="結果", value=result_text, inline=False)
        embed.add_field(name="所持金", value=f"{player_money[uid]}", inline=False)

        # send result as a followup message (ask_bet already deferred)
        await interaction.followup.send(content=f"{interaction.user.mention}", embed=embed, view=PlayAgainView(view.ctx, "horse"))

async def start_horse_race(ctx: commands.Context):
    uid = str(ctx.author.id)
    ensure_user_init(uid)

    horses = random.sample(horsename, 5)
    ranks = []
    for i, h in enumerate(horses):
        others = [horsesize[x] for j, x in enumerate(horses) if j != i]
        ranks.append(calc_horse_rank(h, others))

    embed = discord.Embed(title="🏇 レース出走馬", color=0x3498db)
    embed.description = f"{ctx.author.mention} 出走馬一覧："

    for i, h in enumerate(horses):
        winrate = round((1 / ranks[i]) * 100, 2) if ranks[i] != 0 else 0.0
        embed.add_field(name=f"{i+1}番 {h}", value=f"倍率: {ranks[i]} 倍 / 勝率: {winrate}%", inline=False)

    # send list and then show horse selection view
    msg = await ctx.send(embed=embed)
    view = HorseSelectView(ctx, horses, ranks)
    await ctx.send(f"{ctx.author.mention} どの馬に賭けますか？（番号を選択）", view=view)

# =========================
# ポーカー (UI)
# =========================
suits_p = ['♠', '♥', '♦', '♣']
ranks_p = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']

poker_hand_value = {
    "ロイヤルストレートフラッシュ": 100,
    "ストレートフラッシュ": 50,
    "フォーカード": 25,
    "フルハウス": 10,
    "フラッシュ": 7,
    "ストレート": 5,
    "スリーカード": 3,
    "ツーペア": 2,
    "ワンペア": 1,
    "ハイカード": 0
}

def create_deck_poker():
    return [(s, r) for s in suits_p for r in ranks_p]

def format_hand_text(hand: List[Tuple[str,str]], selected: Optional[Set[int]] = None) -> str:
    if selected is None:
        selected = set()
    txt = ""
    for i, (s, r) in enumerate(hand):
        mark = "🔵" if i in selected else "⚪"
        txt += f"{mark} **{i+1}. {s}{r}**\n"
    return txt

def evaluate_poker_hand(hand: List[Tuple[str,str]]) -> str:
    suits_in_hand = [s for s, r in hand]
    ranks_in_hand = [r for s, r in hand]
    values = sorted([ranks_p.index(r) + 2 for r in ranks_in_hand])

    is_flush = len(set(suits_in_hand)) == 1
    is_straight = all(values[i] + 1 == values[i+1] for i in range(4))
    if values == [2,3,4,5,14]:
        is_straight = True

    count = {v: values.count(v) for v in set(values)}
    counts = sorted(count.values(), reverse=True)

    if is_flush and values == [10,11,12,13,14]:
        return "ロイヤルストレートフラッシュ"
    if is_flush and is_straight:
        return "ストレートフラッシュ"
    if counts == [4,1]:
        return "フォーカード"
    if counts == [3,2]:
        return "フルハウス"
    if is_flush:
        return "フラッシュ"
    if is_straight:
        return "ストレート"
    if counts == [3,1,1]:
        return "スリーカード"
    if counts == [2,2,1]:
        return "ツーペア"
    if counts == [2,1,1,1]:
        return "ワンペア"
    return "ハイカード"

class PokerView(View):
    def __init__(self, ctx, deck: List[Tuple[str,str]], hand: List[Tuple[str,str]]):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.user = ctx.author
        self.deck = deck
        self.hand = hand
        self.original_hand = list(hand)  # store pre-exchange log
        self.selected: Set[int] = set()

        # card buttons
        self.add_item(PokerCardButton("1", 0))
        self.add_item(PokerCardButton("2", 1))
        self.add_item(PokerCardButton("3", 2))
        self.add_item(PokerCardButton("4", 3))
        self.add_item(PokerCardButton("5", 4))
        # exchange button
        self.add_item(PokerExchangeButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("これはあなたのゲームではありません。", ephemeral=True)
            return False
        return True

    def disable_all_items(self):
        for it in list(self.children):
            try:
                it.disabled = True
            except Exception:
                pass

class PokerCardButton(Button):
    def __init__(self, label: str, idx: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: PokerView = self.view  # type: ignore
        if self.idx in view.selected:
            view.selected.remove(self.idx)
        else:
            view.selected.add(self.idx)

        embed = discord.Embed(title="🎴 ポーカー（交換選択）", description=format_hand_text(view.hand, view.selected), color=0x3498db)
        # show pre-exchange hand as well (log)
        embed.add_field(name="交換前の手札", value=format_hand_text(view.original_hand), inline=False)
        # Edit the message showing view (this is the interaction response)
        await interaction.response.edit_message(content=f"{interaction.user.mention}", embed=embed, view=view)

class PokerExchangeButton(Button):
    def __init__(self):
        super().__init__(label="交換する", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        view: PokerView = self.view  # type: ignore

        # perform exchange
        for idx in sorted(view.selected):
            if view.deck:
                view.hand[idx] = view.deck.pop()

        # disable buttons on original view and update message (so users can't spam)
        view.disable_all_items()
        try:
            await interaction.message.edit(view=view)
        except Exception:
            # ignore if message not editable
            pass

        # Now ask bet via interaction (ask_bet_via_interaction will call defer)
        bet = await ask_bet_via_interaction(interaction, view.user.id)
        if bet is None:
            # canceled or timeout
            return

        # compute result and pay
        result = evaluate_poker_hand(view.hand)
        mult = poker_hand_value[result]
        payout = round(bet * mult)

        uid = str(view.user.id)
        ensure_user_init(uid)
        # stake was NOT deducted earlier; we subtract stake and then add payout
        player_money[uid] = player_money.get(uid, 10000) - bet + payout
        await save_money_async(player_money)

        embed = discord.Embed(title="🎉 ポーカー結果", description=format_hand_text(view.hand), color=0xf1c40f)
        embed.add_field(name="交換前（ログ）", value=format_hand_text(view.original_hand), inline=False)
        embed.add_field(name="役", value=result, inline=True)
        embed.add_field(name="倍率", value=f"x{mult}", inline=True)
        embed.add_field(name="賭け金", value=str(bet), inline=True)
        embed.add_field(name="獲得額", value=str(payout), inline=True)
        embed.add_field(name="所持金", value=str(player_money[uid]), inline=False)

        # We already deferred earlier inside ask_bet_via_interaction, so use followup to send the result
        await interaction.followup.send(content=f"{interaction.user.mention}", embed=embed, view=PlayAgainView(view.ctx, "poker"))

async def start_poker(ctx: commands.Context):
    uid = str(ctx.author.id)
    ensure_user_init(uid)

    deck = create_deck_poker()
    random.shuffle(deck)
    hand = [deck.pop() for _ in range(5)]

    embed = discord.Embed(title="🎴 ポーカー開始", description=format_hand_text(hand), color=0x3498db)
    embed.add_field(name="説明", value="交換したいカードを選んでください（複数可）。選択後に［交換する］を押します。交換後に賭け金を選びます。", inline=False)
    view = PokerView(ctx, deck, hand)
    await ctx.send(f"{ctx.author.mention}", embed=embed, view=view)

# ========== Common commands ==========
@bot.command(name="money")
async def money_cmd(ctx):
    uid = str(ctx.author.id)
    ensure_user_init(uid)
    await ctx.send(f"{ctx.author.mention} 所持金：{player_money.get(uid,10000)}")

@bot.command(name="rank")
async def rank_cmd(ctx):
    sorted_players = sorted(player_money.items(), key=lambda x: x[1], reverse=True)
    txt = f"{ctx.author.mention} 🏆 ミナミトリウム ランキング TOP10\n"
    for i, (uid, m) in enumerate(sorted_players[:10], start=1):
        try:
            user_obj = bot.get_user(int(uid))
            name = user_obj.name if user_obj else uid
        except Exception:
            name = uid
        txt += f"{i}位 {name} — {m}\n"
    await ctx.send(txt)

@bot.command(name="resetmoney")
async def resetmoney_cmd(ctx):
    uid = str(ctx.author.id)
    await ctx.send(f"{ctx.author.mention} 本当に所持金を10000に初期化しますか？ (yes/no)")

    def check(m):
        return m.author == ctx.author and m.content.lower() in ["yes", "no"]

    try:
        msg = await bot.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        return await ctx.send(f"{ctx.author.mention} 時間切れです。")

    if msg.content.lower() == "yes":
        player_money[uid] = 10000
        await save_money_async(player_money)
        await ctx.send(f"{ctx.author.mention} 所持金を初期化しました。")
    else:
        await ctx.send(f"{ctx.author.mention} 初期化をキャンセルしました。")

# ========== game commands entrypoints ==========
@bot.command(name="playhorserace")
async def cmd_play_horse(ctx):
    await start_horse_race(ctx)

@bot.command(name="playpoker")
async def cmd_play_poker(ctx):
    await start_poker(ctx)

# ========== errors ==========
@bot.event
async def on_command_error(ctx, error):
    log.exception("コマンド実行エラー: %s", error)
    try:
        await ctx.send(f"{ctx.author.mention} エラーが発生しました：{error}")
    except Exception:
        pass

@bot.event
async def on_ready():
    log.info("ログインしました: %s", bot.user)

if __name__ == "__main__":
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        log.error("環境変数 TOKEN をセットしてください。")
        raise SystemExit("TOKEN not set")
    bot.run(TOKEN)
