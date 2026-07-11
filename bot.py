import asyncio
import re
import json
import os
import logging
from datetime import datetime, timezone
from urllib.parse import unquote

import discord
from discord.ext import commands, tasks
from discord import app_commands
from playwright.async_api import async_playwright

# =============================================================
# LOGGING - minimal for Railway free tier
# =============================================================
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("bot")
log.setLevel(logging.INFO)

# =============================================================
# CONFIG FROM ENV
# =============================================================
TOKEN = os.environ.get("DISCORD_TOKEN", "")
GRND_SID = os.environ.get("GRND_SID", "")
I18N = os.environ.get("I18N_REDIRECTED", "en")
TARGET_REGION = os.environ.get("TARGET_REGION", "eu")
TARGET_SERVER = os.environ.get("TARGET_SERVER", "2 [eu]")

ALLOWED_IDS_STR = os.environ.get("ALLOWED_USER_IDS", "1107905269037539429,1447452209494364222")
ALLOWED_USER_IDS = set()
for x in ALLOWED_IDS_STR.split(","):
    x = x.strip()
    if x.isdigit():
        ALLOWED_USER_IDS.add(int(x))

NOTIFY_CHANNEL_ID = int(os.environ.get("NOTIFY_CHANNEL_ID", "0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

BASE_URL = "https://grnd.gg"
COMPLAINTS_URL = BASE_URL + "/admin/complaints"
COMP_DETAIL_URL = BASE_URL + "/admin/complaints/eu/"

DISCORD_MSG_LIMIT = 1950
EMBED_FIELD_LIMIT = 1024

# Custom emoji
EMOJI_PENDING = os.environ.get("EMOJI_PENDING", "<a:1000220003:1512462977486557266>")
EMOJI_CLOSED = os.environ.get("EMOJI_CLOSED", "<a:1000220005:1512462980112187512>")
EMOJI_FROM = os.environ.get("EMOJI_FROM", "<a:1000220023:1512463004099416234>")
EMOJI_FROM_VAL = os.environ.get("EMOJI_FROM_VAL", "<a:1000220019:1512462996654391326>")
EMOJI_ABOUT = os.environ.get("EMOJI_ABOUT", "<a:1000220024:1512463006540497017>")
EMOJI_ABOUT_VAL = os.environ.get("EMOJI_ABOUT_VAL", "<a:1000220020:1512463000697700412>")
EMOJI_JUDGE = os.environ.get("EMOJI_JUDGE", "<a:1000220028:1512463010759708704>")

COLOR_PENDING = 0x3fe914
COLOR_CLOSED = 0xff0505

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# =============================================================
# INTENTS
# =============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =============================================================
# STATE
# =============================================================
browser_context = None
browser_instance = None
playwright_instance = None

summary_cache = {"count": 0, "rows": [], "raw_text": "No data yet"}
summary_lock = asyncio.Lock()

last_known_count = -1
notify_enabled = False
notify_lock = asyncio.Lock()


# =============================================================
# BROWSER SETUP
# =============================================================
async def setup_browser():
    global playwright_instance, browser_instance, browser_context
    playwright_instance = await async_playwright().start()
    browser_instance = await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
        ]
    )
    browser_context = await browser_instance.new_context(
        viewport={"width": 1400, "height": 900},
        user_agent=DESKTOP_UA,
    )
    # Inject cookies
    cookies = []
    if GRND_SID:
        cookies.append({
            "name": "grnd_sid",
            "value": GRND_SID,
            "domain": ".grnd.gg",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Strict",
        })
    cookies.append({
        "name": "i18n_redirected",
        "value": I18N,
        "domain": "grnd.gg",
        "path": "/",
        "secure": False,
        "httpOnly": False,
        "sameSite": "Lax",
    })
    if cookies:
        await browser_context.add_cookies(cookies)
    log.info("Browser ready")


async def close_browser():
    global browser_context, browser_instance, playwright_instance
    try:
        if browser_context:
            await browser_context.close()
        if browser_instance:
            await browser_instance.close()
        if playwright_instance:
            await playwright_instance.stop()
    except Exception:
        pass


# =============================================================
# SCRAPING JS - ported from Android app
# =============================================================
SUMMARY_JS = """
() => {
    function txt(el) {
        if (!el) return '';
        return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    }
    function formatDate(raw) {
        if (!raw) return '';
        raw = raw.trim();
        var parts = raw.split(',');
        if (parts.length < 2) return raw;
        var left = parts[0].trim();
        var right = parts[1].trim();
        var d = left.split('.');
        var shortDate = left;
        if (d.length >= 2) shortDate = d[0] + '.' + d[1];
        var shortTime = right.substring(0, 5);
        return shortDate + ' ' + shortTime;
    }
    var result = {count: -1, rows: []};
    var activeDivs = document.querySelectorAll('div.active');
    for (var i = 0; i < activeDivs.length; i++) {
        var t = txt(activeDivs[i]);
        if (t.indexOf('New') !== -1) {
            var m = t.match(/New\\s*\\(\\s*(\\d+)\\s*\\)/i);
            if (m) { result.count = parseInt(m[1], 10); break; }
        }
    }
    var trs = document.querySelectorAll('tbody tr');
    if (result.count === -1) result.count = trs.length;
    for (var r = 0; r < trs.length; r++) {
        var tr = trs[r];
        var tds = tr.querySelectorAll('td');
        var a = tr.querySelector("a[href*='/admin/complaints/']");
        var href = '';
        if (a) href = a.getAttribute('href') || '';
        if (href && href.indexOf('http') !== 0) href = 'https://grnd.gg' + href;
        result.rows.push({
            region: (tds.length > 0 ? txt(tds[0]) : ''),
            server: (tds.length > 1 ? txt(tds[1]) : ''),
            id: (tds.length > 2 ? txt(tds[2]) : ''),
            by: (tds.length > 3 ? txt(tds[3]) : ''),
            about: (tds.length > 4 ? txt(tds[4]) : ''),
            date: (tds.length > 5 ? formatDate(txt(tds[5])) : ''),
            url: href
        });
    }
    return result;
}
"""

DETAIL_JS = """
() => {
    function txt(el) {
        if (!el) return '';
        return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    }
    var result = {};
    var items = document.querySelectorAll('.content-info .item');
    for (var i = 0; i < items.length; i++) {
        var labels = items[i].querySelectorAll('.label');
        var titles = items[i].querySelectorAll('.title');
        var lbl = '', val = '';
        if (labels.length > 0) lbl = txt(labels[0]);
        if (titles.length > 0) val = txt(titles[0]);
        if (lbl === 'Complaint') result.complaintId = val;
        if (lbl === 'Region') result.region = val;
        if (lbl === 'Server') result.server = val;
        if (lbl === 'Complaint from') result.from = val;
        if (lbl === 'Complaint about') result.about = val;
        if (lbl === 'Date of complaint submission') result.date = val;
        if (lbl === 'Date of complaint response') result.responseDate = val;
        if (lbl === 'Complaint reviewed by') result.reviewedBy = val;
    }
    var btns = document.querySelectorAll('.buttons');
    var hasIssueBtn = false;
    for (var b = 0; b < btns.length; b++) {
        if (txt(btns[b]).indexOf('Issue judgment') !== -1) hasIssueBtn = true;
    }
    result.isClosed = !hasIssueBtn;

    var descEl = document.querySelector('.descriptions .flex:first-child .text');
    if (!descEl) descEl = document.querySelector('.descriptions .flex .text');
    var rawDesc = descEl ? (descEl.innerText || descEl.textContent || '') : '';
    var linkPattern = /https?:\\/\\/[^\\s]+/g;
    var foundLinks = rawDesc.match(linkPattern) || [];
    var uniqueLinks = [];
    for (var li = 0; li < foundLinks.length; li++) {
        var lnk = foundLinks[li].trim();
        if (lnk.length > 0) {
            var dup = false;
            for (var u = 0; u < uniqueLinks.length; u++) {
                if (uniqueLinks[u] === lnk) { dup = true; break; }
            }
            if (!dup) uniqueLinks.push(lnk);
        }
    }
    result.descriptionLinks = uniqueLinks;
    var cleanDesc = rawDesc;
    for (var cl = 0; cl < uniqueLinks.length; cl++) {
        cleanDesc = cleanDesc.split(uniqueLinks[cl]).join('');
    }
    result.descriptionClean = cleanDesc.replace(/\\s+/g, ' ').trim();
    result.description = rawDesc.replace(/\\s+/g, ' ').trim();

    var offenderFlexes = document.querySelectorAll('.descriptions .flex');
    var offResp = '';
    if (offenderFlexes.length >= 2) {
        var offText = offenderFlexes[1].querySelector('.text');
        if (offText) offResp = txt(offText);
    }
    result.offenderResponse = offResp;

    var verdictTitle = document.querySelector('.verdict.answer .title');
    if (!verdictTitle) verdictTitle = document.querySelector('.verdict .title');
    var verdictText = verdictTitle ? txt(verdictTitle) : '';
    var adminName = '';
    var adminPrefix = '\\u0430\\u0434\\u043c\\u0438\\u043d\\u0438\\u0441\\u0442\\u0440\\u0430\\u0442\\u043e\\u0440\\u043e\\u043c ';
    var aIdx = verdictText.indexOf(adminPrefix);
    if (aIdx !== -1) adminName = verdictText.substring(aIdx + adminPrefix.length).trim();
    result.adminName = adminName;

    var verdictAnswerDiv = document.querySelector('.verdict.answer');
    var adminReply = '';
    if (verdictAnswerDiv) {
        var vaClone = verdictAnswerDiv.cloneNode(true);
        var removeEls = vaClone.querySelectorAll('.title,.date');
        for (var re = 0; re < removeEls.length; re++) {
            removeEls[re].parentNode.removeChild(removeEls[re]);
        }
        adminReply = (vaClone.innerText || vaClone.textContent || '').trim();
    }
    result.adminReply = adminReply;

    var judgeDateEl = document.querySelector('.verdict.answer .date span');
    if (!judgeDateEl) judgeDateEl = document.querySelector('.verdict.answer .date');
    result.judgmentDate = judgeDateEl ? txt(judgeDateEl) : '';

    var imgContainer = document.querySelector('.descriptions .flex:first-child .files');
    if (!imgContainer) imgContainer = document.querySelector('.descriptions .flex .files');
    var imgLinks = [];
    if (imgContainer) {
        var anchors = imgContainer.querySelectorAll('a[href]');
        for (var ai = 0; ai < anchors.length; ai++) {
            var h = anchors[ai].getAttribute('href') || '';
            if (h.length > 0) imgLinks.push(h);
        }
    }
    result.attachedImages = imgLinks;
    result.url = window.location.href;
    return result;
}
"""

SELECT_DROPDOWN_JS = """
(targetRegion, targetServer) => {
    function txt(el) {
        if (!el) return '';
        return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    }
    function clickEl(el) {
        if (!el) return;
        try { el.click(); } catch(e) {}
        try {
            var ev = document.createEvent('MouseEvents');
            ev.initEvent('click', true, true);
            el.dispatchEvent(ev);
        } catch(e) {}
    }
    function chooseItem(keyword) {
        var items = document.querySelectorAll('.select-component-li');
        for (var i = 0; i < items.length; i++) {
            var t = txt(items[i]);
            if (t === keyword || t.indexOf(keyword) !== -1) {
                clickEl(items[i]);
                return true;
            }
        }
        return false;
    }
    return new Promise((resolve) => {
        var selectors = document.querySelectorAll('.select-component');
        if (selectors.length >= 1) clickEl(selectors[0]);
        setTimeout(() => {
            chooseItem(targetRegion);
            setTimeout(() => {
                var selectors2 = document.querySelectorAll('.select-component');
                if (selectors2.length >= 2) clickEl(selectors2[1]);
                setTimeout(() => {
                    chooseItem(targetServer);
                    setTimeout(() => { resolve(true); }, 2000);
                }, 700);
            }, 800);
        }, 700);
    });
}
"""


# =============================================================
# SCRAPER FUNCTIONS
# =============================================================
async def scrape_summary():
    """Scrape complaints list page. Returns (ok, data_dict_or_error_str)"""
    if not browser_context:
        return False, "Browser not ready"
    page = None
    try:
        page = await browser_context.new_page()
        await page.goto(COMPLAINTS_URL, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        # Select dropdowns
        await page.evaluate(SELECT_DROPDOWN_JS, TARGET_REGION, TARGET_SERVER)
        await page.wait_for_timeout(2000)

        # Extract data
        data = await page.evaluate(SUMMARY_JS)
        return True, data

    except Exception as e:
        return False, str(e)
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def scrape_comp_detail(comp_id):
    """Scrape individual complaint page. Returns (ok, data_dict_or_error_str)"""
    if not browser_context:
        return False, "Browser not ready"
    page = None
    try:
        page = await browser_context.new_page()
        url = COMP_DETAIL_URL + str(comp_id)
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        data = await page.evaluate(DETAIL_JS)
        data["url"] = url
        return True, data

    except Exception as e:
        return False, str(e)
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


# =============================================================
# SUMMARY BUILDERS
# =============================================================
def build_summary_text(data, limit=3):
    count = data.get("count", 0)
    rows = data.get("rows", [])

    server_label = "2"
    if rows and rows[0].get("server"):
        server_label = rows[0]["server"]

    lines = [f"Server: {server_label} | Count: {count}"]

    max_rows = len(rows)
    if limit > 0 and limit < max_rows:
        max_rows = limit

    for i in range(max_rows):
        r = rows[i]
        lines.append(
            f"{r.get('date','')} | by {r.get('by','')} "
            f"on {r.get('about','')} → {r.get('url','')}"
        )

    return "\n".join(lines)


def split_message(text, limit=DISCORD_MSG_LIMIT):
    lines = text.split("\n")
    if len(lines) <= 1:
        return [text]

    header = lines[0]
    parts = []
    current = header

    for entry in lines[1:]:
        test = current + "\n" + entry
        if len(test) > limit:
            if current.strip():
                parts.append(current)
            current = entry
        else:
            current = test

    if current.strip():
        parts.append(current)

    return parts if parts else [text]


# =============================================================
# EMBED BUILDERS
# =============================================================
def truncate(text, limit):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def format_evidence_links(links):
    if not links:
        return ""
    parts = []
    for i, link in enumerate(links):
        parts.append(f"**[Evidence {i+1}]({link})**")
    lines = []
    for i in range(0, len(parts), 2):
        chunk = parts[i:i+2]
        lines.append(" │ ".join(chunk))
    return "\n".join(lines)


def build_comp_embeds(data, comp_id):
    complaint_id = data.get("complaintId", f"# {comp_id}")
    comp_from = data.get("from", "N/A")
    comp_about = data.get("about", "N/A")
    date = data.get("date", "N/A")
    is_closed = data.get("isClosed", False)
    description = data.get("descriptionClean", data.get("description", ""))
    desc_links = data.get("descriptionLinks", [])
    offender_response = data.get("offenderResponse", "")
    admin_name = data.get("adminName", "")
    admin_reply = data.get("adminReply", "")
    judgment_date = data.get("judgmentDate", "")
    attached_images = data.get("attachedImages", [])
    page_url = data.get("url", COMP_DETAIL_URL + str(comp_id))

    if is_closed:
        status_emoji = EMOJI_CLOSED
        embed_color = COLOR_CLOSED
    else:
        status_emoji = EMOJI_PENDING
        embed_color = COLOR_PENDING

    embed = discord.Embed(
        title=f"Complaint: {complaint_id}    Status: {status_emoji}",
        url=page_url,
        color=embed_color
    )

    embed.add_field(
        name=f"{EMOJI_FROM}Complaint From",
        value=f"**{EMOJI_FROM_VAL}{comp_from}**",
        inline=False
    )
    embed.add_field(
        name=f"{EMOJI_ABOUT}Complaint About",
        value=f"**{EMOJI_ABOUT_VAL}{comp_about}**",
        inline=False
    )
    embed.add_field(name="Date", value=f"`{date}`", inline=False)

    if description and description.strip():
        embed.add_field(
            name="Description",
            value=f">>> {truncate(description.strip(), EMBED_FIELD_LIMIT - 20)}",
            inline=False
        )
    else:
        embed.add_field(name="Description", value="*No description*", inline=False)

    if desc_links:
        evidence_text = format_evidence_links(desc_links)
        embed.add_field(
            name="Description Attached Links",
            value=truncate(evidence_text, EMBED_FIELD_LIMIT),
            inline=False
        )

    if offender_response and offender_response.strip():
        embed.add_field(
            name="Offender's Response",
            value=f">>> {truncate(offender_response.strip(), EMBED_FIELD_LIMIT - 20)}",
            inline=False
        )
    else:
        embed.add_field(name="Offender's Response", value="*No response*", inline=False)

    if is_closed and admin_name:
        judge_title = f"{EMOJI_JUDGE}Judgement by {admin_name}"
        if judgment_date:
            judge_title += f"   {judgment_date}"
        if admin_reply and admin_reply.strip():
            embed.add_field(
                name=judge_title,
                value=f">>> {truncate(admin_reply.strip(), EMBED_FIELD_LIMIT - 20)}",
                inline=False
            )
        else:
            embed.add_field(name=judge_title, value="*No judgement text*", inline=False)
    else:
        embed.add_field(
            name=f"{EMOJI_JUDGE}Judgement",
            value='*"The Complaint Has Not Been Closed Yet"*',
            inline=False
        )

    embeds = [embed]

    if attached_images:
        embed.set_image(url=attached_images[0])
        for i in range(1, len(attached_images)):
            img_embed = discord.Embed(url=page_url, color=embed_color)
            img_embed.set_image(url=attached_images[i])
            embeds.append(img_embed)

    return embeds


# =============================================================
# PERMISSION CHECK
# =============================================================
def is_allowed(user_id):
    return user_id in ALLOWED_USER_IDS


# =============================================================
# BACKGROUND TASK
# =============================================================
@tasks.loop(seconds=CHECK_INTERVAL)
async def background_check():
    global last_known_count

    ok, data = await scrape_summary()
    if not ok:
        return

    async with summary_lock:
        summary_cache["count"] = data.get("count", 0)
        summary_cache["rows"] = data.get("rows", [])
        summary_cache["raw_text"] = build_summary_text(data, 3)

    current_count = data.get("count", 0)

    if last_known_count == -1:
        last_known_count = current_count
        return

    async with notify_lock:
        should_notify = notify_enabled

    if current_count > last_known_count and should_notify:
        if NOTIFY_CHANNEL_ID:
            channel = bot.get_channel(NOTIFY_CHANNEL_ID)
            if channel:
                diff = current_count - last_known_count
                try:
                    await channel.send(
                        f"🔥 **{diff} New Complaint(s)!** "
                        f"Total new: **{current_count}**"
                    )
                except Exception:
                    pass

    if current_count != last_known_count:
        last_known_count = current_count


@background_check.before_loop
async def before_bg():
    await bot.wait_until_ready()


# =============================================================
# BOT EVENTS
# =============================================================
@bot.event
async def on_ready():
    log.info(f"Logged in: {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} commands")
    except Exception as e:
        log.warning(f"Sync failed: {e}")

    await setup_browser()

    if not background_check.is_running():
        background_check.start()


# =============================================================
# MESSAGE COMMANDS
# =============================================================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    lower = content.lower()

    # jb command
    jb_match = False
    jb_param = None

    if lower in ("!jb", "jb"):
        jb_match = True
    elif lower.startswith("!jb "):
        jb_match = True
        jb_param = content[4:].strip()
    elif lower.startswith("jb "):
        jb_match = True
        jb_param = content[3:].strip()

    if jb_match:
        if not is_allowed(message.author.id):
            await message.channel.send("No access")
            return

        async with summary_lock:
            cached = dict(summary_cache)

        if jb_param and jb_param.lower() == "all":
            text = build_summary_text(cached, -1)
        elif jb_param:
            try:
                n = int(jb_param)
                text = build_summary_text(cached, n)
            except ValueError:
                await message.channel.send("Invalid parameter")
                return
        else:
            text = cached.get("raw_text", "No data")

        parts = split_message(text)
        for p in parts:
            await message.channel.send(p)
        return

    # comp command
    comp_match = False
    comp_id = None

    if lower.startswith("!comp "):
        comp_match = True
        comp_id = content[6:].strip()
    elif lower.startswith("comp "):
        comp_match = True
        comp_id = content[5:].strip()

    if comp_match:
        if not is_allowed(message.author.id):
            await message.channel.send("No access")
            return

        comp_id = re.sub(r'[^0-9]', '', comp_id or '')
        if not comp_id:
            await message.channel.send("Usage: `!comp 324136`")
            return

        loading_msg = await message.channel.send(f"Loading complaint **#{comp_id}**...")

        ok, data = await scrape_comp_detail(comp_id)

        try:
            await loading_msg.delete()
        except Exception:
            pass

        if not ok:
            await message.channel.send(f"Error: {data}")
        else:
            embeds = build_comp_embeds(data, comp_id)
            batch_size = 10
            for i in range(0, len(embeds), batch_size):
                batch = embeds[i:i+batch_size]
                await message.channel.send(embeds=batch)
        return

    await bot.process_commands(message)


# =============================================================
# SLASH COMMANDS
# =============================================================
@bot.tree.command(name="jb", description="Show complaint summary")
@app_commands.describe(count="Number of complaints or 'all' (default: 3)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def jb_slash(interaction: discord.Interaction, count: str = None):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    async with summary_lock:
        cached = dict(summary_cache)

    if count and count.lower() == "all":
        text = build_summary_text(cached, -1)
    elif count:
        try:
            n = int(count)
            text = build_summary_text(cached, n)
        except ValueError:
            await interaction.followup.send("Invalid parameter")
            return
    else:
        text = cached.get("raw_text", "No data")

    parts = split_message(text)
    await interaction.followup.send(parts[0])

    if len(parts) > 1:
        if interaction.guild:
            channel = interaction.channel
        else:
            channel = interaction.user.dm_channel
            if not channel:
                channel = await interaction.user.create_dm()
        for extra in parts[1:]:
            await channel.send(extra)


@bot.tree.command(name="comp", description="Show specific complaint details")
@app_commands.describe(complaint_id="The complaint ID number")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def comp_slash(interaction: discord.Interaction, complaint_id: str):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return

    clean_id = re.sub(r'[^0-9]', '', complaint_id)
    if not clean_id:
        await interaction.response.send_message("Invalid complaint ID", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    ok, data = await scrape_comp_detail(clean_id)

    if not ok:
        await interaction.followup.send(f"Error: {data}")
    else:
        embeds = build_comp_embeds(data, clean_id)
        batch_size = 10
        first_batch = embeds[:batch_size]
        await interaction.followup.send(embeds=first_batch)

        if len(embeds) > batch_size:
            if interaction.guild:
                channel = interaction.channel
            else:
                channel = interaction.user.dm_channel
                if not channel:
                    channel = await interaction.user.create_dm()
            for i in range(batch_size, len(embeds), batch_size):
                batch = embeds[i:i+batch_size]
                await channel.send(embeds=batch)


@bot.tree.command(name="on", description="Enable new complaint notifications")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def notify_on(interaction: discord.Interaction):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return

    global notify_enabled
    async with notify_lock:
        notify_enabled = True

    await interaction.response.send_message(
        "✅ Notifications **ON** — New complaints will be sent to the configured channel.",
        ephemeral=False
    )


@bot.tree.command(name="off", description="Disable new complaint notifications")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def notify_off(interaction: discord.Interaction):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return

    global notify_enabled
    async with notify_lock:
        notify_enabled = False

    await interaction.response.send_message(
        "🔴 Notifications **OFF** — Bot stays online, commands still work.",
        ephemeral=False
    )


@bot.tree.command(name="status", description="Check bot and monitoring status")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def status_cmd(interaction: discord.Interaction):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return

    async with notify_lock:
        n_state = notify_enabled

    async with summary_lock:
        count = summary_cache.get("count", 0)

    await interaction.response.send_message(
        f"**Bot Status**\n"
        f"• Browser: {'✅ Ready' if browser_context else '❌ Not ready'}\n"
        f"• Notifications: {'✅ ON' if n_state else '🔴 OFF'}\n"
        f"• Cached complaint count: **{count}**\n"
        f"• Check interval: **{CHECK_INTERVAL}s**\n"
        f"• Notify channel: `{NOTIFY_CHANNEL_ID}`",
        ephemeral=True
    )


# =============================================================
# CLEANUP
# =============================================================
@bot.event
async def on_close():
    await close_browser()


# =============================================================
# RUN
# =============================================================
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not set")
        exit(1)
    if not GRND_SID:
        print("WARNING: GRND_SID not set - scraping will fail")

    bot.run(TOKEN, log_handler=None)
