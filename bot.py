import os
import re
from pathlib import Path
from datetime import datetime
import asyncio

import pdfplumber
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Google Drive imports
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# ===================== CONFIG ===================== #

# Local folder where PDFs will be stored (downloaded from Drive)
PDF_FOLDER = Path("pdfs")

# Folder & files for logs
LOG_FOLDER = Path("logs")
RATING_LOG = LOG_FOLDER / "ratings.txt"
FEEDBACK_LOG = LOG_FOLDER / "feedback.txt"

# For feedback messages
FEEDBACK_PREFIX = "FB:"


def get_drive_folder_id() -> str:
    folder_id = os.getenv("DRIVE_FOLDER_ID")
    if not folder_id:
        raise RuntimeError(
            "DRIVE_FOLDER_ID not found in .env file. "
            "Add it like: DRIVE_FOLDER_ID=your_folder_id_here"
        )
    return folder_id


# ===================== LOGGING HELPERS ===================== #

def ensure_log_folder():
    LOG_FOLDER.mkdir(exist_ok=True)


def log_rating(user, rating: str):
    ensure_log_folder()
    username = user.username or ""
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    line = (
        f"{datetime.now().isoformat()} | user_id={user.id} | "
        f"name={full_name} | username={username} | rating={rating}\n"
    )
    with open(RATING_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def log_feedback(user, feedback: str):
    ensure_log_folder()
    username = user.username or ""
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    line = (
        f"{datetime.now().isoformat()} | user_id={user.id} | "
        f"name={full_name} | username={username} | feedback={feedback}\n"
    )
    with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(line)


# ===================== TELEGRAM KEYBOARDS ===================== #

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📋 MENU"), KeyboardButton("ℹ️ INFO")],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def rating_keyboard() -> ReplyKeyboardMarkup:
    # Buttons send "1*", "2*", ..., "5*"
    keyboard = [[KeyboardButton(f"{i}*") for i in range(1, 6)]]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ===================== GOOGLE DRIVE SYNC ===================== #

def sync_pdfs_from_drive() -> bool:
    """
    Sync all PDFs from a specific Google Drive folder into the local `pdfs` folder.
    - Creates `pdfs/` if it doesn't exist
    - Removes old PDFs in `pdfs/`
    - Downloads all current PDFs from the Drive folder
    Returns:
        True  -> at least one PDF found & synced
        False -> no PDFs in Drive folder
    """
    print("🔁 Syncing PDFs from Google Drive...")
    PDF_FOLDER.mkdir(exist_ok=True)

    gauth = GoogleAuth()  # uses settings.yaml automatically

    # Try to load saved credentials
    try:
        gauth.LoadCredentialsFile("credentials.json")
    except Exception:
        pass

    if gauth.credentials is None:
        # First time: open browser for login
        print("🌐 No credentials found, running LocalWebserverAuth...")
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        print("🔄 Access token expired, refreshing...")
        gauth.Refresh()
    else:
        print("✅ Credentials loaded, authorizing...")
        gauth.Authorize()

    # Save credentials for next runs
    gauth.SaveCredentialsFile("credentials.json")

    drive = GoogleDrive(gauth)
    folder_id = get_drive_folder_id()

    # Query: all non-trashed PDFs in this folder
    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    file_list = drive.ListFile({"q": query}).GetList()

    # Clear existing local PDFs (mirror behavior)
    for local_pdf in PDF_FOLDER.glob("*.pdf"):
        try:
            local_pdf.unlink()
        except Exception as e:
            print(f"⚠️ Could not delete {local_pdf}: {e}")

    # If no PDFs in Drive folder, signal it
    if not file_list:
        print("⚠️ No PDFs found in the Drive folder.")
        return False

    # Download each PDF
    for gfile in file_list:
        file_name = gfile.get("title") or gfile.get("name") or f"{gfile['id']}.pdf"
        local_path = PDF_FOLDER / file_name
        print(f"⬇️ Downloading: {file_name} -> {local_path}")
        gfile.GetContentFile(str(local_path))

    print("✅ PDF sync finished.")
    return True


# ===================== PDF PARSING HELPERS ===================== #

def parse_time(text: str) -> str:
    """
    Try to extract something like:
      Slot B (11:30 AM - 01:00 PM)
    or:
      Slot B 11:30 AM - 01:00 PM
    from arbitrary text.
    """
    # Pattern 1: Slot B (11:30 AM - 01:00 PM)
    m = re.search(r"Slot\s*:?\.?\s*([A-Z])\s*\(([^)]+)\)", text, re.IGNORECASE)
    if m:
        return f"Slot {m.group(1).upper()} ({m.group(2).strip()})"

    # Pattern 2: Slot B 11:30 AM - 01:00 PM
    m = re.search(
        r"Slot\s*:?\.?\s*([A-Z])\s+([0-9]{1,2}:[0-9]{2}\s*[AP]M\s*[-–]\s*[0-9]{1,2}:[0-9]{2}\s*[AP]M)",
        text,
        re.IGNORECASE,
    )
    if m:
        return f"Slot {m.group(1).upper()} ({m.group(2).strip()})"

    return ""


def parse_date(text: str) -> str:
    """
    Extracts date and converts to: 05-11-2025 (Wednesday)

    Looks for: Date: 05-11-2025 or Date: 05/11/2025
    """
    m = re.search(r"Date\s*:\s*([0-9]{2}[-/][0-9]{2}[-/][0-9]{4})", text, re.IGNORECASE)
    if not m:
        return ""

    date_raw = m.group(1).replace("/", "-")
    try:
        dt = datetime.strptime(date_raw, "%d-%m-%Y")
        weekday = dt.strftime("%A")
        return f"{date_raw} ({weekday})"
    except ValueError:
        # if parsing fails, just return the raw date
        return date_raw


def parse_exam_type(text: str) -> str:
    """
    Detects whether the PDF is for Midterm / Final / Improvement etc.
    Tries to be robust to spacing and capitalization.
    """
    # normalize whitespace + lowercase
    t = " ".join((text or "").lower().split())
    if not t:
        return ""

    # Final exam / final examination / term final etc.
    if (
        "final" in t
        and ("exam" in t or "examination" in t or "term final" in t or "final term" in t)
    ):
        return "Final Examination"

    # Midterm / mid-term / mid term examination
    if (
        ("mid" in t or "midterm" in t or "mid-term" in t or "mid term" in t)
        and ("exam" in t or "examination" in t or "mid term" in t or "term mid" in t)
    ):
        return "Midterm Examination"

    # Improvement / makeup / supplementary
    if any(word in t for word in ["improvement", "improve", "makeup", "make-up", "supplementary"]):
        return "Improvement / Makeup Examination"

    return ""



def parse_course_info(text: str, section_prefix: str):
    """
    Find a line like:
    'FSIT CSE227 Systems Analysis and Design NS 66_A 208 27 50'
    and return: ('Systems Analysis and Design', 'CSE227')
    """
    for line in text.split("\n"):
        # must contain a section like 66_A, 69_B, etc.
        if not re.search(rf"\b{section_prefix}_[A-Z]\b", line):
            continue

        # find course code like CSE227, MAT102, STA101, etc.
        m = re.search(r"\b([A-Z]{3}\d{3,4})\b", line)
        if not m:
            continue

        course_id = m.group(1)
        parts = line.split()
        idx = parts.index(course_id)

        # collect everything after course_id as "raw title + extra"
        title_tokens = parts[idx + 1:]

        # cut off anything from the section code onwards (e.g. 66_A 208 27 50)
        for i, tok in enumerate(title_tokens):
            if re.fullmatch(rf"{section_prefix}_[A-Z]", tok):
                title_tokens = title_tokens[:i]
                break

        raw_title = " ".join(title_tokens).strip()

        # remove trailing initials like NS / nNS
        tokens = raw_title.split()
        while tokens and re.fullmatch(r"[A-Za-z]{2,4}", tokens[-1]) and any(
            c.isupper() for c in tokens[-1]
        ):
            tokens.pop()

        course_name = " ".join(tokens)

        # manual fix for broken 'Design' in this PDF
        if course_name.endswith("Desig"):
            course_name = course_name + "n"

        return course_name, course_id

    return "", ""


def extract_all_section_infos(folder: Path, section_code: str):
    results = []
    section_prefix = section_code.split("_")[0]  # "64", "65", "66", "69", etc.

    for pdf_path in folder.glob("*.pdf"):
        with pdfplumber.open(pdf_path) as pdf:
            time_str = ""      # last seen slot in this file
            date_str = ""
            course_name = ""
            course_id = ""
            exam_type = ""

            num_pages = len(pdf.pages)

            # Try to detect exam type from first page header
            if num_pages > 0:
                first_text = pdf.pages[0].extract_text() or ""
                exam_type = parse_exam_type(first_text)

            # use index so we can also look at next page (for split sections)
            for page_index in range(num_pages):
                page = pdf.pages[page_index]
                text = page.extract_text() or ""

                # date once per file
                if "date" in text.lower() and not date_str:
                    maybe_date = parse_date(text)
                    if maybe_date:
                        date_str = maybe_date

                # 🔹 UPDATED TIME LOGIC (per page / group)
                if "slot" in text.lower():
                    maybe_time = parse_time(text)
                    if maybe_time:
                        time_str = maybe_time   # update last seen slot
                page_time_str = time_str       # slot for THIS page

                # course info once per file
                if not course_id:
                    cn, cid = parse_course_info(text, section_prefix)
                    if cid:
                        course_name, course_id = cn, cid

                # only process pages that contain this section (start)
                if section_code not in text:
                    continue

                # also include next page text to catch overflow rooms
                combined_text = text
                if page_index + 1 < num_pages:
                    next_text = pdf.pages[page_index + 1].extract_text() or ""
                    combined_text = text + "\n" + next_text

                lines = combined_text.split("\n")
                capturing = False
                teacher = ""
                total_seats = ""
                rooms = []

                for line in lines:
                    # start of section row, e.g. "ASM 65_O 801B 3 51"
                    if section_code in line and not capturing:
                        capturing = True
                        parts = line.split()

                        idx = parts.index(section_code)
                        teacher = parts[idx - 1]        # initials before section
                        total_seats = parts[-1]         # last number on line

                        # first room + seats can be here too
                        if idx + 3 < len(parts):
                            room = parts[idx + 1]
                            seats = parts[idx + 2]
                            rooms.append((room, seats))
                        continue

                    if capturing:
                        # stop when ANY new section appears that is NOT this one
                        # e.g. line has 64_A, 65_M, 69_B, etc.
                        if re.search(r"\b\d{2}_[A-Z]\b", line) and section_code not in line:
                            break

                        # more general room pattern: handles 516, 517A, G26, 501A-L, etc.
                        m = re.match(r"^\s*([\w-]+)\s+(\d+)\s*$", line)
                        if m:
                            room, seats = m.groups()

                            # skip grand total line like "Total 1506"
                            if room.lower() == "total":
                                continue

                            rooms.append((room, seats))

                if rooms and teacher:
                    results.append({
                        "date": date_str,
                        "time": page_time_str,   # ✅ page-specific slot
                        "section": section_code,
                        "teacher": teacher,
                        "rooms": rooms,
                        "total": total_seats,
                        "course_name": course_name,
                        "course_id": course_id,
                        "exam_type": exam_type,
                    })
                    # assume one occurrence of this section per file
                    break

    return results


def format_section_infos(section_code: str) -> str:
    # 🔁 Always sync latest PDFs from Google Drive before searching
    try:
        has_files = sync_pdfs_from_drive()
    except Exception as e:
        return (
            "⚠️ Failed to sync PDFs from Google Drive.\n"
            f"Error: `{e}`\n"
            "Make sure `client_secrets.json`, `settings.yaml`, and `DRIVE_FOLDER_ID` are set correctly."
        )

    # If there are no PDFs at all in Drive
    if not has_files:
        return (
            "🛠️ Maintenance ongoing or routine coming soon.\n"
            "Currently no routine PDF files are available in the system."
        )

    infos = extract_all_section_infos(PDF_FOLDER, section_code)

    if not infos:
        return f"❌ No occurrences of section `{section_code}` found."

    blocks = []

    # serials: (i), (ii), (iii) ...
    roman_map = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi", 7: "vii", 8: "viii", 9: "ix", 10: "x"}

    for idx, info in enumerate(infos, start=1):
        roman = roman_map.get(idx, str(idx))
        block = []
        block.append(f"({roman}) ==== 📚 SECTION `{info['section']}` ====")

        # Exam type if detected
        if info.get("exam_type"):
            block.append(f"📝 Exam: {info['exam_type']}")

        if info["date"]:
            block.append(f"📅 Date: {info['date']}")
        else:
            block.append("📅 Date: (not found)")

        if info["time"]:
            block.append(f"⏰ {info['time']}\n")
        else:
            block.append("⏰ Time / Slot: (not found)\n")

        block.append(f"📘 Course: {info['course_name']} ({info['course_id']})")
        block.append(f"👨‍🏫 Teacher: {info['teacher']}\n")

        block.append("🏫 Rooms & Seats:")
        for room, seats in info["rooms"]:
            block.append(f"- Room {room} — {seats}")

        block.append(f"\n🧮 Total Seats: {info['total']}")
        block.append("===============X==============")

        blocks.append("\n".join(block))

    return "\n\n".join(blocks)


# ===================== TELEGRAM BOT PART ===================== #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hi, I am *MR ROUTINE*.\n"
        "Welcome! I can find your DIU exam routine by section.\n\n"
        "👉 Just send me your *section code* in this format:\n"
        "`66_A`, `69_K`, `64_B`, etc.\n\n"
        "I'll sync the latest routine PDFs and show you the exam info.\n"
        "Use /info to learn more about this bot."
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ *MR ROUTINE — Bot Info*\n\n"
        "This bot was developed to quickly find DIU exam routines for specific sections by reading "
        "official PDF routine files from Google Drive.\n\n"
        "🔧 *How it works*\n"
        "- Syncs the latest routine PDFs from a Drive folder\n"
        "- Scans them for your section code (like `66_A`)\n"
        "- Extracts exam type, date, time slot, course, teacher, rooms and total seats\n\n"
        "⚠️ *Disclaimer*\n"
        "- The bot may make mistakes while reading PDFs.\n"
        "- Always double-check with the official routine from your department.\n\n"
        "© MR ROUTINE\n"
        "👨‍💻 Developer: Sifatur Rahman iMRAN.\n"
        "This bot is made for educational and personal use to save time before exams."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())


async def handle_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    section_input = (update.message.text or "").strip().upper()

    # ----- Menu buttons -----
    if section_input == "📋 MENU":
        await start(update, context)
        return

    if section_input == "ℹ️ INFO":
        await info(update, context)
        return

    # ----- Rating handling -----
    if re.fullmatch(r"[1-5]\*", section_input):
        log_rating(user, section_input)
        await update.message.reply_text(
            f"🙏 Thank you for rating MR ROUTINE {section_input}!",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ----- Feedback handling -----
    if section_input.startswith(FEEDBACK_PREFIX):
        feedback = section_input[len(FEEDBACK_PREFIX):].strip()
        if not feedback:
            await update.message.reply_text(
                "✍️ Please write your feedback after `FB:`.\n"
                "Example: `FB: Please also add day-wise filter.`",
                parse_mode="Markdown",
            )
        else:
            log_feedback(user, feedback)
            await update.message.reply_text(
                "💌 Thanks for your feedback! It has been noted.",
                reply_markup=main_menu_keyboard(),
            )
        return

    # ----- Section handling -----

    # basic validation: must look like NN_X
    if not re.fullmatch(r"\d{2}_[A-Z]", section_input):
        await update.message.reply_text(
            "⚠️ Please send a valid section code like `66_A` or `69_K`.\n\n"
            "For feedback, start your message with `FB:`.\n"
            "For rating, reply with `1*`, `2*`, `3*`, `4*` or `5*`.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        "🔍 Syncing PDFs & searching, please wait...\n\n"
        "⚠️ *Disclaimer:* This bot may make mistakes while reading PDFs. "
        "Always double-check with the official routine.",
        parse_mode="Markdown",
    )

    # run the heavy PDF + Drive work in a separate thread
    result_text = await asyncio.to_thread(format_section_infos, section_input)

    await update.message.reply_text(result_text, parse_mode="Markdown")

    # Thank you + rating/feedback options
    thank_text = (
        "🙏 *Thank you for using MR ROUTINE!* 🫶\n\n"
        "⭐ *Rating:*\n"
        "Tap a button or reply with `1*`, `2*`, `3*`, `4*` or `5*` to rate this bot.\n\n"
        "💬 *Feedback:*\n"
        "Send a message starting with `FB:` followed by your feedback.\n"
        "Example:\n"
        "`FB: Please also add day-wise filter or some problem found.`"
    )
    await update.message.reply_text(
        thank_text,
        parse_mode="Markdown",
        reply_markup=rating_keyboard(),
    )


def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise RuntimeError("BOT_TOKEN not found in .env file")

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("info", info))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_section))

    print("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
