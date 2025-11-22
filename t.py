import os
import time
import logging
import asyncio
import re
import shutil
import pathlib
import json
import glob
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ForceReply

# ---------------- CONFIGURATION ----------------
# I have inserted your credentials here as defaults.
API_ID = int(os.getenv("API_ID", 27972068))
API_HASH = os.getenv("API_HASH", "6e7e2f5cdddba536b8e603b3155223c1")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7027917459:AAG2jKW2hqkYaJj2Zuhw5bcTXNYhpDotGzQ")

# Tuning
CHUNK_SIZE = 512 * 1024 
SPLIT_SIZE_BYTES = 1900 * 1024 * 1024 # 1.9GB limit
WORKDIR = pathlib.Path("downloads")
WORKDIR.mkdir(exist_ok=True)

# State Management
USER_STATE = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UltimateBot")

app = Client(
    "ultimate_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=50,
    #ipv6=True
)

# ---------------- FFMPEG TOOLS ----------------

async def split_video(input_path: str, output_prefix: str):
    """Splits video into 1.9GB parts without re-encoding."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path, 
        "-c", "copy", "-map", "0", "-f", "segment", "-segment_format", "mp4",
        "-fs", str(SPLIT_SIZE_BYTES), "-reset_timestamps", "1", 
        f"{output_prefix}%03d.mp4"
    ]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return sorted(glob.glob(f"{output_prefix}*.mp4"))

async def merge_videos(video_list: list, output_path: str):
    """Merges videos using concat demuxer."""
    list_file = WORKDIR / f"merge_list_{int(time.time())}.txt"
    with open(list_file, "w") as f:
        for vid in video_list:
            f.write(f"file '{os.path.abspath(vid)}'\n")
    
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", output_path]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    if os.path.exists(list_file): os.remove(list_file)
    return os.path.exists(output_path)

async def take_screenshot(input_path, output_path, timestamp):
    cmd = ["ffmpeg", "-y", "-ss", timestamp, "-i", input_path, "-vframes", "1", "-q:v", "2", output_path]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return os.path.exists(output_path)

async def extract_audio(input_path, output_path):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", output_path]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()

async def make_gif(input_path, output_path):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", "scale=320:-1:flags=lanczos,fps=10", "-t", "5", "-f", "gif", output_path]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return os.path.exists(output_path) and os.path.getsize(output_path) < 2097152

# ---------------- BOT LOGIC ----------------

@app.on_message(filters.command("start"))
async def start(c, m):
    await m.reply_text(
        "🤖 **Ultimate Media Bot Online.**\n\n"
        "I am the all-in-one tool:\n"
        "🔹 **Split** (>2GB files)\n"
        "🔹 **Merge** (Stitch videos)\n"
        "🔹 **Rename & Convert**\n"
        "🔹 **Thumbnails & Screenshots**\n\n"
        "**Send a file to begin.**"
    )

@app.on_message(filters.command("done"))
async def done_merge(c, m):
    uid = m.from_user.id
    if uid not in USER_STATE or USER_STATE[uid]["action"] != "merge_mode":
        await m.reply_text("❌ You aren't in merge mode. Click '➕ Merge' first.")
        return
        
    files = USER_STATE[uid]["files"]
    if len(files) < 2:
        await m.reply_text("❌ Send at least 2 videos.")
        return

    status = await m.reply_text(f"🔗 **Merging {len(files)} videos...**")
    out_path = WORKDIR / f"merged_{uid}.mp4"
    
    try:
        if await merge_videos(files, str(out_path)):
            await m.reply_video(str(out_path), caption="**✨ Merged!**")
            await status.delete()
        else:
            await status.edit("❌ Merge failed. Ensure videos are same format.")
    except Exception as e:
        await status.edit(f"Error: {e}")
    finally:
        for f in files: os.remove(f)
        if out_path.exists(): os.remove(out_path)
        del USER_STATE[uid]

@app.on_message(filters.video | filters.document | filters.audio)
async def main_handler(c, m: Message):
    uid = m.from_user.id
    
    # 1. Merge Mode Collection
    if uid in USER_STATE and USER_STATE[uid]["action"] == "merge_mode":
        if not (m.video or (m.document and "video" in m.document.mime_type)):
            await m.reply_text("❌ Send VIDEOS only for merging.")
            return
        
        path = WORKDIR / f"merge_{uid}_{len(USER_STATE[uid]['files'])}.mp4"
        msg = await m.reply_text("📥 **Added to Queue...**")
        await m.download(str(path))
        USER_STATE[uid]["files"].append(str(path))
        await msg.edit_text(f"✅ **Video #{len(USER_STATE[uid]['files'])} Added.**\nType **/done** to finish.")
        return

    # 2. Thumbnail Collection
    if m.photo and uid in USER_STATE and USER_STATE[uid]["action"] == "wait_thumb":
        return # Handled by photo handler

    # 3. Main Menu
    fname = m.video.file_name if m.video else (m.document.file_name if m.document else "file")
    buttons = [
        [InlineKeyboardButton("📝 Rename", "act:rename"), InlineKeyboardButton("🎵 To MP3", "act:audio")],
        [InlineKeyboardButton("➕ Merge", "act:merge_start"), InlineKeyboardButton("🔪 Split (>2GB)", "act:split")],
        [InlineKeyboardButton("🎞 GIF", "act:gif"), InlineKeyboardButton("📸 Screenshot", "act:ss")],
        [InlineKeyboardButton("🖼 Set Thumb", "act:thumb")]
    ]
    await m.reply_text(f"**File:** `{fname}`\nSelect Operation:", reply_markup=InlineKeyboardMarkup(buttons), quote=True)

@app.on_callback_query(filters.regex("^act:"))
async def callbacks(c, cb: CallbackQuery):
    act = cb.data.split(":")[1]
    msg = cb.message.reply_to_message
    uid = cb.from_user.id

    if act == "merge_start":
        await cb.answer()
        USER_STATE[uid] = {"action": "merge_mode", "files": []}
        await cb.message.edit_text("🔗 **Merge Mode On.**\nSend videos one by one.\nType **/done** when finished.")
        return

    if not msg:
        await cb.answer("❌ File lost.", show_alert=True)
        return

    if act == "split":
        await cb.answer("Checking size...")
        status = await cb.message.reply_text("📥 **Downloading...**")
        dl = WORKDIR / f"big_{uid}.mp4"
        prefix = WORKDIR / f"part_{uid}_"
        try:
            await msg.download(str(dl))
            if os.path.getsize(dl) < SPLIT_SIZE_BYTES:
                await status.edit("🤔 File is small (<1.9GB). Sending back.")
                await c.send_document(cb.message.chat.id, str(dl))
            else:
                await status.edit("🔪 **Splitting...**")
                parts = await split_video(str(dl), str(prefix))
                await status.edit(f"📦 **Uploading {len(parts)} parts...**")
                for i, p in enumerate(parts):
                    await c.send_document(cb.message.chat.id, p, caption=f"Part {i+1}")
                    os.remove(p)
                await status.delete()
        except Exception as e:
            await status.edit(f"Error: {e}")
        finally:
            if dl.exists(): os.remove(dl)

    elif act == "audio":
        await cb.answer("Extracting...")
        status = await cb.message.reply_text("🎵 **Converting...**")
        dl = WORKDIR / f"v_{uid}.mp4"
        out = WORKDIR / f"a_{uid}.mp3"
        try:
            await msg.download(str(dl))
            await extract_audio(str(dl), str(out))
            await c.send_audio(cb.message.chat.id, str(out))
            await status.delete()
        except Exception as e:
            await status.edit(f"Error: {e}")
        finally:
            if dl.exists(): os.remove(dl)
            if out.exists(): os.remove(out)

    elif act == "gif":
        await cb.answer("Making GIF...")
        # Logic similar to previous, simplified for length
        status = await cb.message.reply_text("🎞 **Cooking GIF...**")
        dl = WORKDIR / f"g_{uid}.mp4"
        out = WORKDIR / f"g_{uid}.gif"
        try:
            await msg.download(str(dl))
            if await make_gif(str(dl), str(out)):
                await c.send_animation(cb.message.chat.id, str(out))
                await status.delete()
            else:
                await status.edit("❌ GIF failed (too big).")
        finally:
            if dl.exists(): os.remove(dl)
            if out.exists(): os.remove(out)

    elif act == "rename":
        await cb.answer()
        USER_STATE[uid] = {"action": "wait_name", "msg": msg}
        await cb.message.reply_text("📝 **New Name?**", reply_markup=ForceReply())

    elif act == "ss":
        await cb.answer()
        USER_STATE[uid] = {"action": "wait_ts", "msg": msg}
        await cb.message.reply_text("⏱ **Timestamp?** (e.g. 00:01:30)", reply_markup=ForceReply())

    elif act == "thumb":
        await cb.answer()
        USER_STATE[uid] = {"action": "wait_thumb", "msg": msg}
        await cb.message.reply_text("🖼 **Send a Photo.**", reply_markup=ForceReply())

@app.on_message(filters.text & filters.private)
async def inputs(c, m):
    uid = m.from_user.id
    if uid not in USER_STATE: return
    
    st = USER_STATE[uid]
    if st["action"] == "wait_name":
        path = WORKDIR / m.text.replace("/", "_")
        status = await m.reply_text("📝 **Renaming...**")
        try:
            await st["msg"].download(str(path))
            await m.reply_document(str(path), caption=f"📄 `{m.text}`")
            await status.delete()
        finally:
            if path.exists(): os.remove(path)
            del USER_STATE[uid]

    elif st["action"] == "wait_ts":
        ts = m.text.replace(" call on ", ":").replace(".", ":")
        status = await m.reply_text("📸 **Capturing...**")
        dl = WORKDIR / f"s_{uid}.mp4"
        out = WORKDIR / f"s_{uid}.jpg"
        try:
            await st["msg"].download(str(dl))
            if await take_screenshot(str(dl), str(out), ts):
                await m.reply_photo(str(out), caption=f"Time: {ts}")
                await status.delete()
            else:
                await status.edit("❌ Invalid timestamp.")
        finally:
            if dl.exists(): os.remove(dl)
            if out.exists(): os.remove(out)
            del USER_STATE[uid]

@app.on_message(filters.photo & filters.private)
async def photo_handler(c, m):
    uid = m.from_user.id
    if uid in USER_STATE and USER_STATE[uid]["action"] == "wait_thumb":
        status = await m.reply_text("🖼 **Applying...**")
        vid = WORKDIR / f"v_{uid}.mp4"
        th = WORKDIR / f"t_{uid}.jpg"
        try:
            await asyncio.gather(USER_STATE[uid]["msg"].download(str(vid)), m.download(str(th)))
            await c.send_video(m.chat.id, str(vid), thumb=str(th), caption="**New Thumbnail!**")
            await status.delete()
        finally:
            if vid.exists(): os.remove(vid)
            if th.exists(): os.remove(th)
            del USER_STATE[uid]

if __name__ == "__main__":
    print("🚀 Bot Started with your credentials.")
    app.run()
