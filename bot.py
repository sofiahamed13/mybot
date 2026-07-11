import re
import os
import json
import asyncio
import logging
import warnings
from urllib.parse import quote

import discord
from discord.ext import commands
from discord import app_commands
from playwright.async_api import async_playwright

# suppress PyNaCl warning
warnings.filterwarnings("ignore", message="PyNaCl is not installed")
logging.getLogger("discord.voice_client").setLevel(logging.CRITICAL)

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("bot")

# =============================================================
# CONFIG
# =============================================================
TOKEN         = os.environ.get("DISCORD_TOKEN", "").strip()
GRND_SID      = os.environ.get("GRND_SID", "").strip()
I18N          = os.environ.get("I18N_REDIRECTED", "en").strip()
TARGET_REGION = os.environ.get("TARGET_REGION", "eu").strip()
TARGET_SERVER = os.environ.get("TARGET_SERVER", "2 [eu]").strip()

ALLOWED_IDS_STR = os.environ.get(
    "ALLOWED_USER_IDS",
    "1107905269037539429,1362246929467183326"
)
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in ALLOWED_IDS_STR.split(",")
    if x.strip().isdigit()
}

BASE_URL        = "https://grnd.gg"
COMPLAINTS_URL  = BASE_URL + "/admin/complaints"
COMP_DETAIL_URL = BASE_URL + "/admin/complaints/eu/"

DISCORD_MSG_LIMIT = 1950
EMBED_FIELD_LIMIT = 1024

# =============================================================
# EMOJI CONFIG ← animated emoji এখানে replace করুন
# =============================================================
EMOJI_PENDING   = os.environ.get("EMOJI_PENDING",   "⏳")
EMOJI_CLOSED    = os.environ.get("EMOJI_CLOSED",    "🔒")
EMOJI_FROM      = os.environ.get("EMOJI_FROM",      "👤")
EMOJI_FROM_VAL  = os.environ.get("EMOJI_FROM_VAL",  "▸")
EMOJI_ABOUT     = os.environ.get("EMOJI_ABOUT",     "🎯")
EMOJI_ABOUT_VAL = os.environ.get("EMOJI_ABOUT_VAL", "▸")
EMOJI_JUDGE     = os.environ.get("EMOJI_JUDGE",     "⚖️")

COLOR_PENDING = 0x3FE914
COLOR_CLOSED  = 0xFF0505

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# =============================================================
# DISCORD BOT
# =============================================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =============================================================
# GLOBAL STATE
# =============================================================
playwright_instance = None
browser_instance    = None
browser_context     = None
scrape_lock         = None

# =============================================================
# HELPERS
# =============================================================
def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


def truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit - 3] + "..."


def split_message(text: str, limit: int = DISCORD_MSG_LIMIT):
    lines = text.split("\n")
    if len(lines) <= 1:
        return [text]
    header  = lines[0]
    parts   = []
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


def format_evidence_links(links):
    if not links:
        return ""
    parts = [f"**[Evidence {i+1}]({link})**" for i, link in enumerate(links)]
    lines = []
    for i in range(0, len(parts), 2):
        lines.append(" │ ".join(parts[i:i+2]))
    return "\n".join(lines)


def build_summary_text(data, limit=3):
    count = data.get("count", 0)
    rows  = data.get("rows", [])
    server_label = "2"
    if rows and rows[0].get("server"):
        server_label = rows[0]["server"]
    lines    = [f"Server: {server_label} | Count: {count}"]
    max_rows = len(rows)
    if isinstance(limit, int) and 0 < limit < max_rows:
        max_rows = limit
    for i in range(max_rows):
        row = rows[i]
        lines.append(
            f"{row.get('date','')} | by {row.get('by','')} "
            f"on {row.get('about','')} → {row.get('url','')}"
        )
    return "\n".join(lines)


def build_comp_embeds(data, comp_id):
    complaint_id      = data.get("complaintId", f"# {comp_id}")
    comp_from         = data.get("from", "N/A")
    comp_about        = data.get("about", "N/A")
    date              = data.get("date", "N/A")
    is_closed         = data.get("isClosed", False)
    description       = data.get("descriptionClean", data.get("description", ""))
    desc_links        = data.get("descriptionLinks", [])
    offender_response = data.get("offenderResponse", "")
    admin_name        = data.get("adminName", "")
    admin_reply       = data.get("adminReply", "")
    judgment_date     = data.get("judgmentDate", "")
    attached_images   = data.get("attachedImages", [])
    page_url          = data.get("url", COMP_DETAIL_URL + str(comp_id))

    status_emoji = EMOJI_CLOSED if is_closed else EMOJI_PENDING
    embed_color  = COLOR_CLOSED if is_closed else COLOR_PENDING

    embed = discord.Embed(
        title=f"Complaint: {complaint_id}    Status: {status_emoji}",
        url=page_url, color=embed_color
    )
    embed.add_field(
        name=f"{EMOJI_FROM} Complaint From",
        value=f"**{EMOJI_FROM_VAL} {comp_from}**", inline=False
    )
    embed.add_field(
        name=f"{EMOJI_ABOUT} Complaint About",
        value=f"**{EMOJI_ABOUT_VAL} {comp_about}**", inline=False
    )
    embed.add_field(name="Date", value=f"`{date}`", inline=False)
    embed.add_field(
        name="Description",
        value=(f">>> {truncate(description.strip(), EMBED_FIELD_LIMIT - 20)}"
               if description and description.strip() else "*No description*"),
        inline=False
    )
    if desc_links:
        embed.add_field(
            name="Description Attached Links",
            value=truncate(format_evidence_links(desc_links), EMBED_FIELD_LIMIT),
            inline=False
        )
    embed.add_field(
        name="Offender's Response",
        value=(f">>> {truncate(offender_response.strip(), EMBED_FIELD_LIMIT - 20)}"
               if offender_response and offender_response.strip() else "*No response*"),
        inline=False
    )
    if is_closed and admin_name:
        judge_title = f"{EMOJI_JUDGE} Judgement by {admin_name}"
        if judgment_date:
            judge_title += f"   {judgment_date}"
        embed.add_field(
            name=judge_title,
            value=(f">>> {truncate(admin_reply.strip(), EMBED_FIELD_LIMIT - 20)}"
                   if admin_reply and admin_reply.strip() else "*No judgement text*"),
            inline=False
        )
    else:
        embed.add_field(
            name=f"{EMOJI_JUDGE} Judgement",
            value='*"The Complaint Has Not Been Closed Yet"*', inline=False
        )

    embeds = [embed]
    if attached_images:
        embed.set_image(url=attached_images[0])
        for img_url in attached_images[1:]:
            img_embed = discord.Embed(url=page_url, color=embed_color)
            img_embed.set_image(url=img_url)
            embeds.append(img_embed)
    return embeds


async def send_embeds_batched(target, embeds):
    if not embeds:
        return
    for i in range(0, len(embeds), 10):
        await target.send(embeds=embeds[i:i+10])

# =============================================================
# COOKIE HELPERS
# =============================================================
def extract_server_number(server_text: str):
    m = re.search(r"(\d+)", server_text or "")
    return int(m.group(1)) if m else None


def build_filter_cookies():
    cookies = []
    server_number = extract_server_number(TARGET_SERVER)
    if GRND_SID:
        cookies.append({
            "name": "grnd_sid", "value": GRND_SID,
            "domain": ".grnd.gg", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Strict",
        })
    cookies.append({
        "name": "i18n_redirected", "value": I18N,
        "domain": "grnd.gg", "path": "/",
        "secure": False, "httpOnly": False, "sameSite": "Lax",
    })
    cookies.append({
        "name": "filters:/admin/complaints:region",
        "value": quote(json.dumps([TARGET_REGION]), safe=""),
        "domain": "grnd.gg", "path": "/",
        "secure": False, "httpOnly": False, "sameSite": "Lax",
    })
    if server_number is not None:
        cookies.append({
            "name": "filters:/admin/complaints:server",
            "value": quote(
                json.dumps({TARGET_REGION: [server_number]}, separators=(",", ":")),
                safe=""
            ),
            "domain": "grnd.gg", "path": "/",
            "secure": False, "httpOnly": False, "sameSite": "Lax",
        })
    return cookies

# =============================================================
# PLAYWRIGHT JS
# =============================================================
SELECT_DROPDOWN_JS = r"""
(args) => {
    const targetRegion = args.targetRegion;
    const targetServer = args.targetServer;
    function txt(el){ return el?(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim():''; }
    function clickEl(el){
        if(!el)return;
        try{el.click();}catch(e){}
        try{const ev=document.createEvent('MouseEvents');ev.initEvent('click',true,true);el.dispatchEvent(ev);}catch(e){}
    }
    function chooseItem(keyword){
        const items=document.querySelectorAll('.select-component-li');
        for(let i=0;i<items.length;i++){
            const t=txt(items[i]);
            if(t===keyword||t.indexOf(keyword)!==-1){clickEl(items[i]);return true;}
        }
        return false;
    }
    return new Promise((resolve)=>{
        const selectors=document.querySelectorAll('.select-component');
        if(selectors.length>=1)clickEl(selectors[0]);
        setTimeout(()=>{
            chooseItem(targetRegion);
            setTimeout(()=>{
                const selectors2=document.querySelectorAll('.select-component');
                if(selectors2.length>=2)clickEl(selectors2[1]);
                setTimeout(()=>{
                    chooseItem(targetServer);
                    setTimeout(()=>resolve(true),1500);
                },600);
            },700);
        },600);
    });
}
"""

SUMMARY_JS = r"""
() => {
    function txt(el){ return el?(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim():''; }
    function formatDate(raw){
        if(!raw)return'';raw=raw.trim();
        const parts=raw.split(',');if(parts.length<2)return raw;
        const left=parts[0].trim();const right=parts[1].trim();
        const d=left.split('.');let shortDate=left;
        if(d.length>=2)shortDate=d[0]+'.'+d[1];
        return shortDate+' '+right.substring(0,5);
    }
    const result={count:-1,rows:[]};
    const activeDivs=document.querySelectorAll('div.active');
    for(let i=0;i<activeDivs.length;i++){
        const t=txt(activeDivs[i]);
        if(t.indexOf('New')!==-1){
            const m=t.match(/New\s*\(\s*(\d+)\s*\)/i);
            if(m){result.count=parseInt(m[1],10);break;}
        }
    }
    const trs=document.querySelectorAll('tbody tr');
    if(result.count===-1)result.count=trs.length;
    for(let r=0;r<trs.length;r++){
        const tr=trs[r];const tds=tr.querySelectorAll('td');
        const a=tr.querySelector("a[href*='/admin/complaints/']");
        let href='';
        if(a)href=a.getAttribute('href')||'';
        if(href&&href.indexOf('http')!==0)href='https://grnd.gg'+href;
        result.rows.push({
            region:(tds.length>0?txt(tds[0]):''),
            server:(tds.length>1?txt(tds[1]):''),
            id:(tds.length>2?txt(tds[2]):''),
            by:(tds.length>3?txt(tds[3]):''),
            about:(tds.length>4?txt(tds[4]):''),
            date:(tds.length>5?formatDate(txt(tds[5])):''),
            url:href
        });
    }
    return result;
}
"""

DETAIL_JS = r"""
() => {
    function txt(el){ return el?(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim():''; }
    const result={};
    const items=document.querySelectorAll('.content-info .item');
    for(let i=0;i<items.length;i++){
        const labels=items[i].querySelectorAll('.label');
        const titles=items[i].querySelectorAll('.title');
        let lbl='',val='';
        if(labels.length>0)lbl=txt(labels[0]);
        if(titles.length>0)val=txt(titles[0]);
        if(lbl==='Complaint')result.complaintId=val;
        if(lbl==='Region')result.region=val;
        if(lbl==='Server')result.server=val;
        if(lbl==='Complaint from')result.from=val;
        if(lbl==='Complaint about')result.about=val;
        if(lbl==='Date of complaint submission')result.date=val;
        if(lbl==='Date of complaint response')result.responseDate=val;
        if(lbl==='Complaint reviewed by')result.reviewedBy=val;
    }
    const btns=document.querySelectorAll('.buttons');
    let hasIssueBtn=false;
    for(let b=0;b<btns.length;b++){if(txt(btns[b]).indexOf('Issue judgment')!==-1)hasIssueBtn=true;}
    result.isClosed=!hasIssueBtn;
    let descEl=document.querySelector('.descriptions .flex:first-child .text');
    if(!descEl)descEl=document.querySelector('.descriptions .flex .text');
    const rawDesc=descEl?(descEl.innerText||descEl.textContent||''):'';
    const linkPattern=/https?:\/\/[^\s]+/g;
    const foundLinks=rawDesc.match(linkPattern)||[];
    const uniqueLinks=[];
    for(let i=0;i<foundLinks.length;i++){
        const link=foundLinks[i].trim();
        if(link&&!uniqueLinks.includes(link))uniqueLinks.push(link);
    }
    result.descriptionLinks=uniqueLinks;
    let cleanDesc=rawDesc;
    for(let i=0;i<uniqueLinks.length;i++)cleanDesc=cleanDesc.split(uniqueLinks[i]).join('');
    result.descriptionClean=cleanDesc.replace(/\s+/g,' ').trim();
    result.description=rawDesc.replace(/\s+/g,' ').trim();
    const offenderFlexes=document.querySelectorAll('.descriptions .flex');
    let offResp='';
    if(offenderFlexes.length>=2){const offText=offenderFlexes[1].querySelector('.text');if(offText)offResp=txt(offText);}
    result.offenderResponse=offResp;
    let verdictTitle=document.querySelector('.verdict.answer .title');
    if(!verdictTitle)verdictTitle=document.querySelector('.verdict .title');
    const verdictText=verdictTitle?txt(verdictTitle):'';
    let adminName='';
    const adminPrefix='администратором ';
    const idx=verdictText.indexOf(adminPrefix);
    if(idx!==-1)adminName=verdictText.substring(idx+adminPrefix.length).trim();
    result.adminName=adminName;
    const verdictAnswerDiv=document.querySelector('.verdict.answer');
    let adminReply='';
    if(verdictAnswerDiv){
        const clone=verdictAnswerDiv.cloneNode(true);
        const removeEls=clone.querySelectorAll('.title,.date');
        for(let i=0;i<removeEls.length;i++)removeEls[i].parentNode.removeChild(removeEls[i]);
        adminReply=(clone.innerText||clone.textContent||'').trim();
    }
    result.adminReply=adminReply;
    let judgeDateEl=document.querySelector('.verdict.answer .date span');
    if(!judgeDateEl)judgeDateEl=document.querySelector('.verdict.answer .date');
    result.judgmentDate=judgeDateEl?txt(judgeDateEl):'';
    let imgContainer=document.querySelector('.descriptions .flex:first-child .files');
    if(!imgContainer)imgContainer=document.querySelector('.descriptions .flex .files');
    const imgLinks=[];
    if(imgContainer){
        const anchors=imgContainer.querySelectorAll('a[href]');
        for(let i=0;i<anchors.length;i++){const h=anchors[i].getAttribute('href')||'';if(h.length>0)imgLinks.push(h);}
    }
    result.attachedImages=imgLinks;
    result.url=window.location.href;
    return result;
}
"""

# =============================================================
# BROWSER SETUP
# =============================================================
async def setup_browser():
    global playwright_instance, browser_instance, browser_context, scrape_lock

    scrape_lock = asyncio.Lock()

    playwright_instance = await async_playwright().start()
    browser_instance = await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--single-process",
        ]
    )
    browser_context = await browser_instance.new_context(
        viewport={"width": 1400, "height": 900},
        user_agent=DESKTOP_UA,
    )
    cookies = build_filter_cookies()
    if cookies:
        await browser_context.add_cookies(cookies)

    log.info("Browser ready")

# =============================================================
# SCRAPERS (on-demand only)
# =============================================================
async def live_scrape_summary():
    if not browser_context:
        return False, "Browser not ready"
    page = None
    try:
        async with scrape_lock:
            page = await browser_context.new_page()
            await page.goto(COMPLAINTS_URL, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(1800)
            await page.evaluate(
                SELECT_DROPDOWN_JS,
                {"targetRegion": TARGET_REGION, "targetServer": TARGET_SERVER}
            )
            await page.wait_for_timeout(2000)
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


async def live_scrape_comp(comp_id):
    if not browser_context:
        return False, "Browser not ready"
    page = None
    try:
        async with scrape_lock:
            page = await browser_context.new_page()
            url  = COMP_DETAIL_URL + str(comp_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2500)
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
# EVENTS
# =============================================================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")

    if browser_context is None:
        await setup_browser()

    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.warning(f"Slash sync failed: {e}")

# =============================================================
# MESSAGE COMMANDS (!jb / jb  and  !comp / comp)
# =============================================================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    lower   = content.lower()

    # ─── !jb / jb ─────────────────────────────────────────
    jb_match, jb_param = False, None
    if lower in ("!jb", "jb"):
        jb_match = True
    elif lower.startswith("!jb "):
        jb_match, jb_param = True, content[4:].strip()
    elif lower.startswith("jb "):
        jb_match, jb_param = True, content[3:].strip()

    if jb_match:
        if not is_allowed(message.author.id):
            await message.channel.send("No access")
            return
        loading = await message.channel.send("⏳ Fetching live data...")
        ok, data = await live_scrape_summary()
        try:
            await loading.delete()
        except Exception:
            pass
        if not ok:
            await message.channel.send(f"Error: {data}")
            return
        if jb_param and jb_param.lower() == "all":
            text = build_summary_text(data, -1)
        elif jb_param:
            try:
                text = build_summary_text(data, int(jb_param))
            except ValueError:
                await message.channel.send("Invalid parameter")
                return
        else:
            text = build_summary_text(data, 3)
        for part in split_message(text):
            await message.channel.send(part)
        return

    # ─── !comp / comp ─────────────────────────────────────
    comp_match, comp_id = False, None
    if lower.startswith("!comp "):
        comp_match, comp_id = True, content[6:].strip()
    elif lower.startswith("comp "):
        comp_match, comp_id = True, content[5:].strip()

    if comp_match:
        if not is_allowed(message.author.id):
            await message.channel.send("No access")
            return
        comp_id = re.sub(r"[^0-9]", "", comp_id or "")
        if not comp_id:
            await message.channel.send("Usage: `!comp 324136`")
            return
        loading = await message.channel.send(f"⏳ Loading complaint **#{comp_id}**...")
        ok, data = await live_scrape_comp(comp_id)
        try:
            await loading.delete()
        except Exception:
            pass
        if not ok:
            await message.channel.send(f"Error: {data}")
            return
        await send_embeds_batched(message.channel, build_comp_embeds(data, comp_id))
        return

    await bot.process_commands(message)

# =============================================================
# SLASH COMMANDS (only /jb and /comp)
# =============================================================
@bot.tree.command(name="jb", description="Show complaint summary (live)")
@app_commands.describe(count="Number of complaints or 'all' (default: 3)")
async def jb_slash(interaction: discord.Interaction, count: str = None):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    ok, data = await live_scrape_summary()
    if not ok:
        await interaction.followup.send(f"Error: {data}")
        return
    if count and count.lower() == "all":
        text = build_summary_text(data, -1)
    elif count:
        try:
            text = build_summary_text(data, int(count))
        except ValueError:
            await interaction.followup.send("Invalid parameter")
            return
    else:
        text = build_summary_text(data, 3)
    parts = split_message(text)
    await interaction.followup.send(parts[0])
    for extra in parts[1:]:
        await interaction.channel.send(extra)


@bot.tree.command(name="comp", description="Show specific complaint details (live)")
@app_commands.describe(complaint_id="The complaint ID number")
async def comp_slash(interaction: discord.Interaction, complaint_id: str):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("No access", ephemeral=True)
        return
    clean_id = re.sub(r"[^0-9]", "", complaint_id or "")
    if not clean_id:
        await interaction.response.send_message("Invalid complaint ID", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    ok, data = await live_scrape_comp(clean_id)
    if not ok:
        await interaction.followup.send(f"Error: {data}")
        return
    embeds = build_comp_embeds(data, clean_id)
    await interaction.followup.send(embeds=embeds[:10])
    for i in range(10, len(embeds), 10):
        await interaction.channel.send(embeds=embeds[i:i+10])

# =============================================================
# RUN
# =============================================================
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not set")
        raise SystemExit(1)
    if not GRND_SID:
        print("WARNING: GRND_SID not set — scraping will likely fail")
    bot.run(TOKEN, log_handler=None)
