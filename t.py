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

# --- ADMIN CONFIGURATION ---
# IMPORTANT: Replace 123456789 with your actual Telegram User ID (numeric)
OWNER_ID = int(os.getenv("OWNER_ID", 6075512585)) 
# ---------------------------------------------

# Tuning
CHUNK_SIZE = 512 * 1024 
SPLIT_SIZE_BYTES = 1900 * 1024 * 1024 # 1.9GB limit
WORKDIR = pathlib.Path("downloads")
# WORKDIR.mkdir(exist_ok=True) # Removed from here, added to cleanup

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

# ---------------- UTILITY FUNCTIONS ----------------

def format_bytes(size: int) -> str:
    """Formats bytes into human-readable strings (KB, MB, GB)."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"

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

async def convert_video_resolution(input_path: str, output_path: str, height: int):
    """Converts video resolution to a specified height (maintaining aspect ratio) and encodes to h.264 MP4."""
    # Use h.264 (libx264) codec, medium preset, and scale filter
    cmd = [
        "ffmpeg", "-y", "-i", input_path, 
        "-c:v", "libx264", "-crf", "23", "-preset", "medium", 
        "-vf", f"scale=-2:{height}", # -2 ensures the width is an even number, maintaining aspect ratio based on height
        "-c:a", "aac", "-b:a", "128k",
        output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return os.path.exists(output_path)

async def update_metadata(input_path: str, output_path: str, key: str, value: str):
    """Updates a single metadata tag without re-encoding streams."""
    # Use -metadata tag=value and -c copy for fast modification
    cmd = [
        "ffmpeg", "-y", "-i", input_path, 
        "-metadata", f"{key}={value}", 
        "-c", "copy",
        output_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()
    return os.path.exists(output_path)

async def ffprobe_metadata(input_path: str):
    """Extracts stream and format metadata using ffprobe."""
    # ffprobe is used to quickly read the metadata structure
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", input_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        logger.error(f"FFprobe failed: {stderr.decode()}")
        return None
        
    try:
        data = json.loads(stdout.decode())
        
        metadata = {}
        # Format tags (general file metadata)
        if 'tags' in data.get('format', {}):
            metadata['Format Tags (Global)'] = data['format']['tags']
        
        # Stream tags (e.g., video, audio)
        for i, stream in enumerate(data.get('streams', [])):
            if 'tags' in stream:
                stream_type = stream.get('codec_type', f'Stream {i}')
                metadata[f'{stream_type.capitalize()} Stream Tags'] = stream['tags']
                
        return metadata
    except Exception as e:
        logger.error(f"Error parsing FFprobe JSON: {e}")
        return None

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
        "**Send a file to begin.**\n\n"
        f"**Admin Commands:** /restart (Owner ID: `{OWNER_ID}`)"
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

# ---------------- ADMIN COMMANDS ----------------

@app.on_message(filters.command("restart") & filters.user(OWNER_ID))
async def restart_command(client, message):
    """
    Handles the /restart command.
    Uses the provided decorative message but executes a clean exit for restart.
    """
    try:
        # Use the decorative status message before exiting
        await message.reply_text("🔄 **𝙿𝚁𝙾𝙲𝙴𝚂𝚂𝙴𝚂 𝚂𝚃𝙾𝙿𝙴𝙳. 𝙱𝙾𝚃 𝙸𝚂 𝚁𝙴𝚂𝚃𝙰𝚁𝚃𝙸𝙽𝙶...**")
        
        # Stop the Pyrogram client gracefully
        await client.stop()

        # Terminate the Python process. The external process manager (like supervisor) 
        # will detect this exit code (0) and start the bot script again.
        os._exit(0)

    except Exception as e:
        logger.error(f"Error during restart sequence: {e}")
        # Exit with an error code to signal an abnormal termination
        os._exit(1)

# ---------------- MEDIA HANDLERS ----------------

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
        [InlineKeyboardButton("🎞 GIF", "act:gif"), InlineKeyboardButton("📐 Change Res", "act:res")], 
        [InlineKeyboardButton("📸 Screenshot", "act:ss"), InlineKeyboardButton("🖼 Set Thumb", "act:thumb")],
        [InlineKeyboardButton("🏷 Metadata", "act:meta")] 
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
        
    # --- METADATA HANDLING START ---
    if act == "meta": 
        await cb.answer("Processing metadata...")
        status = await cb.message.reply_text("📥 **Downloading file to read metadata...**")
        
        dl = WORKDIR / f"meta_temp_{uid}.mp4" 

        try:
            await msg.download(str(dl))
            
            # Read metadata
            metadata = await ffprobe_metadata(str(dl))
            
            # Store necessary state
            USER_STATE[uid] = {
                "action": "meta_menu", 
                "msg": msg,
                "dl_path": str(dl),
                "metadata": metadata
            }

            buttons = [
                [InlineKeyboardButton("👀 Show Current Tags", "act:meta_show")],
                [InlineKeyboardButton("✍️ Set/Change Tag", "act:meta_set")],
                [InlineKeyboardButton("❌ Cancel", "act:meta_cancel")]
            ]
            
            await status.edit_text(
                "🏷 **Metadata Options**\n\n"
                "You can view the existing metadata or set a new tag (like 'title' or 'artist').", 
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        except Exception as e:
            await status.edit_text(f"❌ Error during metadata download: {e}")
            if dl.exists(): os.remove(dl)
            del USER_STATE[uid]
        return

    elif act == "meta_show":
        st = USER_STATE.get(uid)
        if not st or st['action'] != 'meta_menu':
            await cb.answer("❌ Please start over.", show_alert=True)
            return

        await cb.answer()
        
        metadata = st['metadata']
        if not metadata:
            text = "⚠️ **No readable metadata found.**"
        else:
            meta_str = "**Current Metadata Tags:**\n\n"
            for section, tags in metadata.items():
                if tags:
                    meta_str += f"**{section}:**\n"
                    # Sort keys alphabetically for clean display
                    for k, v in sorted(tags.items()): 
                        # Escape backticks in value if they exist, otherwise Python markdown gets confused
                        safe_v = str(v).replace('`', '`')
                        meta_str += f"`{k}`: `{safe_v}`\n"
                    meta_str += "\n"
            
            # Truncate if too long for a single message
            if len(meta_str) > 4000:
                meta_str = meta_str[:3900] + "\n... (truncated)"
                
            text = meta_str

        # Show metadata and allow user to proceed to set a new tag
        buttons = [[InlineKeyboardButton("✍️ Set/Change Tag", "act:meta_set")]]
        
        # Edit the original message (from act:meta) or reply if it's too old
        try:
             await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except:
             await cb.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        
        return
        
    elif act == "meta_set":
        st = USER_STATE.get(uid)
        if not st or st['action'] != 'meta_menu':
            await cb.answer("❌ Please start over.", show_alert=True)
            return
            
        await cb.answer()
        # Change state to wait for the key
        USER_STATE[uid]["action"] = "wait_meta_key"
        
        await cb.message.reply_text(
            "🔑 **Enter Metadata Key**\n\n"
            "Examples: `title`, `artist`, `comment`, `album`.", 
            reply_markup=ForceReply()
        )
        return

    elif act == "meta_cancel":
        st = USER_STATE.get(uid)
        if st and st.get('dl_path') and os.path.exists(st['dl_path']): 
            os.remove(st['dl_path'])
        if uid in USER_STATE:
            del USER_STATE[uid]
        await cb.message.edit_text("❌ Metadata operation cancelled and temporary file deleted.")
        return
    # --- METADATA HANDLING END ---

    if act == "res": # Resolution Action - Changed to show buttons
        await cb.answer()
        # Show buttons for common resolutions
        buttons = [
            # Low Resolutions
            [InlineKeyboardButton("⬇️ 144p", "res:144"), InlineKeyboardButton("⬇️ 240p", "res:240"), InlineKeyboardButton("⬇️ 360p", "res:360")],
            # Standard Resolutions
            [InlineKeyboardButton("⬇️ 480p", "res:480"), InlineKeyboardButton("⬇️ 720p", "res:720"), InlineKeyboardButton("⬇️ 1080p", "res:1080")],
            # High Resolution
            [InlineKeyboardButton("⬇️ 1440p", "res:1440")],
        ]
            
        await cb.message.edit_text(
            "📐 **Select Output Resolution**\n\n"
            "This will re-encode the video, which may take time. The estimated file size will be shown after conversion.", 
            reply_markup=InlineKeyboardMarkup(buttons + [[InlineKeyboardButton("❌ Cancel", "act:cancel_res")]])
        )
        # Store message reference to prevent issues
        USER_STATE[uid] = {"action": "wait_res_selection", "msg": msg} 
        return

    if act == "cancel_res":
        # Cancel button if user changes mind
        if uid in USER_STATE and USER_STATE[uid].get('action') == 'wait_res_selection':
            del USER_STATE[uid]
        await cb.message.edit_text("📐 Resolution change cancelled.")
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
        USER_STATE[uid] = {"action": "wait_name_input", "msg": msg}
        await cb.message.reply_text("📝 **New Name?**", reply_markup=ForceReply())

    elif act == "ss":
        await cb.answer()
        USER_STATE[uid] = {"action": "wait_ts", "msg": msg}
        await cb.message.reply_text("⏱ **Timestamp?** (e.g. 00:01:30)", reply_markup=ForceReply())

    elif act == "thumb":
        await cb.answer()
        USER_STATE[uid] = {"action": "wait_thumb", "msg": msg}
        await cb.message.reply_text("🖼 **Send a Photo.**", reply_markup=ForceReply())

@app.on_callback_query(filters.regex("^res:"))
async def res_select(c, cb: CallbackQuery):
    _, height_str = cb.data.split(":") # Now expecting height (e.g., 720)
    uid = cb.from_user.id
    height = int(height_str) # Target height

    st = USER_STATE.get(uid)
    if not st or st['action'] != 'wait_res_selection':
        await cb.answer("❌ State expired. Please start over from the main menu.", show_alert=True)
        return
        
    msg = st['msg']
    
    status = await cb.message.edit_text(f"📥 **Downloading & Converting to {height}p...**")
    
    dl = WORKDIR / f"res_in_{uid}.mp4"
    # Use height in the output name
    out_name = f"converted_{height}p_{msg.video.file_name if msg.video else 'file.mp4'}"
    out_path = WORKDIR / out_name
    
    try:
        await msg.download(str(dl))
        await status.edit_text(f"📐 **Converting to {height}p...** This may take a while.")
        
        # Pass height to the conversion function
        if await convert_video_resolution(str(dl), str(out_path), height):
            
            # Calculate file size and format it
            new_size_bytes = os.path.getsize(out_path)
            new_size_formatted = format_bytes(new_size_bytes)
            
            # Transition to format selection state after conversion
            USER_STATE[uid] = {
                "action": "wait_format_selection", 
                "temp_path": str(out_path),
                "new_name": out_name,
            }
            
            buttons = [
                [InlineKeyboardButton("🎥 Send as VIDEO", f"format:video:{uid}")],
                [InlineKeyboardButton("📄 Send as FILE/Document", f"format:document:{uid}")]
            ]
            
            await status.edit_text(
                f"✅ Conversion to {height}p complete.\n"
                f"📦 **New Size:** `{new_size_formatted}`\n\n"
                "**How should I send the converted file?**", 
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        else:
            await status.edit_text("❌ Conversion failed. Check the FFmpeg logs.")
            del USER_STATE[uid]
            
    except Exception as e:
        await status.edit_text(f"❌ Error during conversion: {e}")
        del USER_STATE[uid]
        
    finally:
        if dl.exists(): os.remove(dl)
        # out_path is cleaned up in format_callbacks

@app.on_callback_query(filters.regex("^format:"))
async def format_callbacks(c, cb: CallbackQuery):
    _, act_type, uid_str = cb.data.split(":")
    uid = int(uid_str) # Get the original user ID

    if uid not in USER_STATE or USER_STATE[uid]["action"] != "wait_format_selection":
        await cb.answer("❌ State expired. Please start over.", show_alert=True)
        return

    st = USER_STATE[uid]
    file_path = pathlib.Path(st["temp_path"])
    
    status = await cb.message.edit_text(f"📤 **Uploading as {act_type.upper()}...**")

    try:
        if not file_path.exists():
            raise FileNotFoundError("Temporary file not found.")

        # Determine the Telegram function based on the button clicked
        if act_type == "video":
            await c.send_video(
                cb.message.chat.id, 
                str(file_path), 
                caption=f"🎥 **Renamed/Converted:** `{st['new_name']}`"
            )
        elif act_type == "document":
            await c.send_document(
                cb.message.chat.id, 
                str(file_path), 
                caption=f"📄 **Renamed/Converted:** `{st['new_name']}`"
            )
            
        await status.edit_text("✨ **File Sent!**")

    except Exception as e:
        await status.edit_text(f"❌ Upload failed: {e}")

    finally:
        # Clean up temporary file and state
        if file_path.exists(): 
            os.remove(file_path)
        if uid in USER_STATE:
            del USER_STATE[uid]


@app.on_message(filters.text & filters.private)
async def inputs(c, m):
    uid = m.from_user.id
    if uid not in USER_STATE: return
    
    st = USER_STATE[uid]
    
    # ------------------ 1. RENAME INPUT (New Name) ------------------
    if st["action"] == "wait_name_input":
        new_filename = m.text.replace("/", "_")
        path = WORKDIR / new_filename 
        
        status = await m.reply_text("📥 **Downloading original file...**")
        
        try:
            await st["msg"].download(str(path))
            
            # Transition to format selection state
            USER_STATE[uid] = {
                "action": "wait_format_selection", 
                "temp_path": str(path),
                "new_name": new_filename
            }
            
            buttons = [
                [InlineKeyboardButton("🎥 Send as VIDEO", f"format:video:{uid}")],
                [InlineKeyboardButton("📄 Send as FILE/Document", f"format:document:{uid}")]
            ]
            
            await status.edit_text(
                f"✅ File downloaded as `{new_filename}`.\n\n"
                "**How should I send the renamed file?**", 
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            
        except Exception as e:
            await status.edit_text(f"❌ Error during download/rename: {e}")
            if path.exists(): os.remove(path)
            del USER_STATE[uid]
        
        return 

    # ------------------ 2. SCREENSHOT INPUT (Timestamp) ------------------
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
            
    # ------------------ 3. METADATA KEY INPUT ------------------
    elif st["action"] == "wait_meta_key":
        meta_key = m.text.strip().replace(":", "_").replace("=", "_")
        if not meta_key:
            await m.reply_text("❌ Key cannot be empty. Please try again.")
            return

        USER_STATE[uid]["meta_key"] = meta_key
        USER_STATE[uid]["action"] = "wait_meta_value"
        
        await m.reply_text(
            f"✍️ **Enter Value for `{meta_key}`**\n\n"
            "This will be the new value for the tag.", 
            reply_markup=ForceReply()
        )
        return

    # ------------------ 4. METADATA VALUE INPUT ------------------
    elif st["action"] == "wait_meta_value":
        meta_value = m.text.strip()
        meta_key = st["meta_key"]
        
        status = await m.reply_text(f"🏷 **Updating tag `{meta_key}` to `{meta_value}`...**")
        
        dl_path = st["dl_path"]
        base_name = pathlib.Path(dl_path).stem
        ext = pathlib.Path(dl_path).suffix
        
        # Use a new name for the output file
        out_name = f"{base_name}_meta_edited{ext}"
        out_path = WORKDIR / out_name
        
        try:
            if not os.path.exists(dl_path):
                 raise FileNotFoundError("Original file lost.")
                 
            # 1. Update metadata (This is usually very fast)
            await status.edit_text(f"🔧 **Applying new metadata tag...**")
            success = await update_metadata(str(dl_path), str(out_path), meta_key, meta_value)

            if success:
                # 2. Upload the new file
                await status.edit_text(f"📤 **Uploading edited file...**")
                await c.send_document(
                    m.chat.id, 
                    str(out_path), 
                    caption=f"✅ Metadata updated: `{meta_key}` set to `{meta_value}`"
                )
                await status.delete()
            else:
                await status.edit_text("❌ Metadata update failed.")
            
        except Exception as e:
            await status.edit_text(f"❌ Error during metadata processing: {e}")
            
        finally:
            # Clean up both the original and the new file
            if dl_path and os.path.exists(dl_path): os.remove(dl_path)
            if out_path.exists(): os.remove(out_path)
            
            # Clean up state
            if uid in USER_STATE:
                del USER_STATE[uid]

@app.on_message(filters.photo & filters.private)
async def photo_handler(c, m):
    uid = m.from_user.id
    if uid in USER_STATE and USER_STATE[uid]["action"] == "wait_thumb":
        status = await m.reply_text("🖼 **Applying...**")
        vid_msg = USER_STATE[uid]["msg"] 
        vid = WORKDIR / f"v_{uid}.mp4"
        th = WORKDIR / f"t_{uid}.jpg"
        
        try:
            await asyncio.gather(vid_msg.download(str(vid)), m.download(str(th)))
            
            await c.send_video(m.chat.id, str(vid), thumb=str(th), caption="**New Thumbnail Applied!**")
            await status.delete()
        
        finally:
            if vid.exists(): os.remove(vid)
            if th.exists(): os.remove(th)
            del USER_STATE[uid]

if __name__ == "__main__":
    # --- STARTUP CLEANUP ---
    # Delete the entire downloads folder and recreate it on every bot start
    if WORKDIR.exists():
        logger.info(f"🧹 Cleaning up existing work directory: {WORKDIR}")
        shutil.rmtree(WORKDIR)
    
    WORKDIR.mkdir(exist_ok=True)
    logger.info(f"📂 Work directory created/recreated: {WORKDIR}")
    # -----------------------
    
    logger.info("🚀 Bot Started with your credentials.")
    app.run()
