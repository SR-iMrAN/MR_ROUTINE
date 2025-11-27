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
            "DRIVE_FOLDER_ID not found in environment variables. "
            "Add it in Choreo as DRIVE_FOLDER_ID=your_folder_id_here"
        )
    return folder_id


# ===================== LOGGING HELPERS ===================== #

def ensure_log_folder():
    LOG_FOLDER.mkdir(exist_ok=True)


def log_rating(user, rating: str):
    ensure_log_folder()
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"

    line = f"{datetime.now().isoformat()} | name={full_name} | rating={rating}\n"

    with open(RATING_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def log_feedback(user, feedback: str):
    ensure_log_folder()
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"

    line = f"{datetime.now().isoformat()} | name={full_name} | feedback={feedback}\n"

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
    Returns:
        True  -> at least one PDF found & synced
        False -> no PDFs in Drive folder
    """
    print("🔁 Syncing PDFs from Google Drive...")
    PDF_FOLDER.mkdir(exist_ok=True)

    gauth = GoogleAuth()  # uses settings.yaml automatically

    try:
        gauth.LoadCredentialsFile("credentials.json")
    except Exception:
        pass

    running_in_cloud = bool(os.getenv("WEBHOOK_URL"))

    if gauth.credentials is None:
        if running_in_cloud:
            # In cloud we expect a bundled credentials.json; if it's missing, fail clearly.
            raise RuntimeError(
                "No valid Google Drive credentials found in this cloud environment. "
                "Ensure 'credentials.json' is included in the image or mount it."
            )
        else:
            # Local dev: can use browser auth
            print("🌐 No credentials found, running LocalWebserverAuth (LOCAL USE ONLY)...")
            gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        print("🔄 Access token expired, refreshing...")
        gauth.Refresh()
    else:
        print("✅ Credentials loaded, authorizing...")
        gauth.Authorize()

    # In Choreo the root FS is read-only, so saving may fail.
    try:
        gauth.SaveCredentialsFile("credentials.json")
    except Exception as e:
        print(f"⚠️ Could not save credentials (probably read-only FS): {e}")

    drive = GoogleDrive(gauth)
    folder_id = get_drive_folder_id()

    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    file_list = drive.ListFile({"q": query}).GetList()

    # Clear existing local PDFs
    for local_pdf in PDF_FOLDER.glob("*.pdf"):
        try:
            local_pdf.unlink()
        except Exception as e:
            print(f"⚠️ Could not delete {local_pdf}: {e}")

    if not file_list:
        print("⚠️ No PDFs found in the Drive folder.")
        return False

    for gfile in file_list:
        file_name = gfile.get("title") or gfile.get("name") or f"{gfile['id']}.pdf"
        local_path = PDF_FOLDER / file_name
        print(f"⬇️ Downloading: {file_name} -> {local_path}")
        gfile.GetContentFile(str(local_path))

    print("✅ PDF sync finished.")
    return True


# ===================== PDF PARSING HELPERS ===================== #

def parse_time(text: str) -> str:
    m = re.search(r"Slot\s*:?\.?\s*([A-Z])\s*\(([^)]+)\)", text, re.IGNORECASE)
    if m:
        return f"Slot {m.group(1).upper()} ({m.group(2).strip()})"

    m = re.search(
        r"Slot\s*:?\.?\s*([A-Z])\s+([0-9]{1,2}:[0-9]{2}\s*[AP]M\s*[-–]\s*[0-9]{1,2}:[0-9]{2}\s*[AP]M)",
        text,
        re.IGNORECASE,
    )
    if m:
        return f"Slot {m.group(1).upper()} ({m.group(2).strip()})"

    return ""


def parse_date(text: str) -> str:
    m = re.search(r"Date\s*:\s*([0-9]{2}[-/][0-9]{2}[-/][0-9]{4})", text, re.IGNORECASE)
    if not m:
        return ""

    date_raw = m.group(1).replace("/", "-")
    try:
        dt = datetime.strptime(date_raw, "%d-%m-%Y")
        weekday = dt.strftime("%A")
        return f"{date_raw} ({weekday})"
    except ValueError:
        return date_raw


def parse_exam_type(text: str) -> str:
    t = " ".join((text or "").lower().split())
    if not t:
        return ""

    if (
        "final" in t
        and ("exam" in t or "examination" in t or "term final" in t or "final term" in t)
    ):
        return "Final Examination"

    if (
        ("mid" in t or "midterm" in t or "mid-term" in t or "mid term" in t)
        and ("exam" in t or "examination" in t or "mid term" in t or "term mid" in t)
    ):
        return "Midterm Examination"

    if any(word in t for word in ["improvement", "improve", "makeup", "make-up", "supplementary"]):
        return "Improvement / Makeup Examination"

    return ""


def parse_course_info(text: str, section_prefix: str):
    for line in text.split("\n"):
        if not re.search(rf"\b{section_prefix}_[A-Z]\b", line):
            continue

        m = re.search(r"\b([A-Z]{3}\d{3,4})\b", line)
        if not m:
            continue

        course_id = m.group(1)
        parts = line.split()
        idx = parts.index(course_id)

        title_tokens = parts[idx + 1:]

        for i, tok in enumerate(title_tokens):
            if re.fullmatch(rf"{section_prefix}_[A-Z]", tok):
                title_tokens = title_tokens[:i]
                break

        raw_title = " ".join(title_tokens).strip()

        tokens = raw_title.split()
        while tokens and re.fullmatch(r"[A-Za-z]{2,4}", tokens[-1]) and any(
            c.isupper() for c in tokens[-1]
        ):
            tokens.pop()

        course_name = " ".join(tokens)

        if course_name.endswith("Desig"):
            course_name = course_name + "n"

        return course_name, course_id

    return "", ""


def extract_all_section_infos(folder: Path, section_code: str):
    results = []
    section_prefix = section_code.split("_")[0]

    for pdf_path in folder.glob("*.pdf"):
        with pdfplumber.open(pdf_path) as pdf:
            time_str = ""
            date_str = ""
            course_name = ""
            course_id = ""
            exam_type = ""

            num_pages = len(pdf.pages)

            if num_pages > 0:
                first_text = pdf.pages[0].extract_text() or ""
                exam_type = parse_exam_type(first_text)

            for page_index in range(num_pages):
                page = pdf.pages[page_index]
                text = page.extract_text() or ""

                if "date" in text.lower() and not date_str:
                    maybe_date = parse_date(text)
                    if maybe_date:
                        date_str = maybe_date

                if "slot" in text.lower():
                    maybe_time = parse_time(text)
                    if maybe_time:
                        time_str = maybe_time
                page_time_str = time_str

                if not course_id:
                    cn, cid = parse_course_info(text, section_prefix)
                    if cid:
                        course_name, course_id = cn, cid

                if section_code not in text:
                    continue

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
                    if section_code in line and not capturing:
                        capturing = True
                        parts = line.split()

                        idx = parts.index(section_code)
                        teacher = parts[idx - 1]
                        total_seats = parts[-1]

                        if idx + 3 < len(parts):
                            room = parts[idx + 1]
                            seats = parts[idx + 2]
                            rooms.append((room, seats))
                        continue

                    if capturing:
                        if re.search(r"\b\d{2}_[A-Z]\b", line) and section_code not in line:
                            break

                        m = re.match(r"^\s*([\w-]+)\s+(\d+)\s*$", line)
                        if m:
                            room, seats = m.groups()

                            if room.lower() == "total":
                                continue

                            rooms.append((room, seats))

                if rooms and teacher:
                    results.append({
                        "date": date_str,
                        "time": page_time_str,
                        "section": section_code,
                        "teacher": teacher,
                        "rooms": rooms,
                        "total": total_seats,
                        "course_name": course_name,
                        "course_id": course_id,
                        "exam_type": exam_type,
                    })
                    break

    return results


def format_section_infos(section_code: str) -> str:
    try:
        has_files = sync_pdfs_from_drive()
    except Exception as e:
        return (
            "⚠️ Failed to sync PDFs from Google Drive.\n"
            f"Error: `{e}`\n"
            "Make sure `client_secrets.json`, `settings.yaml`, credentials, "
            "and `DRIVE_FOLDER_ID` are configured correctly."
        )

    if not has_files:
        return (
            "🛠️ Maintenance ongoing or routine coming soon.\n"
            "Currently no routine PDF files are available in the system."
        )

    infos = extract_all_section_infos(PDF_FOLDER, section_code)

    if not infos:
        return f"❌ No occurrences of section `{section_code}` found."

    blocks = []
    roman_map = {
        1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v",
        6: "vi", 7: "vii", 8: "viii", 9: "ix", 10: "x"
    }

    for idx, info in enumerate(infos, start=1):
        roman = roman_map.get(idx, str(idx))
        block = []
        block.append(f"({roman}) ==== 📚 SECTION `{info['section']}` ====")

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
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def handle_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    section_input = (update.message.text or "").strip().upper()

    if section_input == "📋 MENU":
        await start(update, context)
        return

    if section_input == "ℹ️ INFO":
        await info(update, context)
        return

    if re.fullmatch(r"[1-5]\*", section_input):
        log_rating(user, section_input)
        await update.message.reply_text(
            f"🙏 Thank you for rating MR ROUTINE {section_input}!",
            reply_markup=main_menu_keyboard(),
        )
        return

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

    result_text = await asyncio.to_thread(format_section_infos, section_input)

    await update.message.reply_text(result_text, parse_mode="Markdown")

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


# ===================== TELEGRAM BOT ENTRYPOINT ===================== #

def main():
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not found in environment variables")

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("info", info))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_section))

    webhook_url = os.getenv("WEBHOOK_URL")  # e.g. https://.../default/mrroutine/v1.0
    port = int(os.getenv("PORT", "8000"))

    if webhook_url:
        # ▶ Choreo / webhook mode
        print(f"Webhook mode enabled -> {webhook_url}")

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,  # internal path (ServicePath="/<token>")
            webhook_url=f"{webhook_url.rstrip('/')}/{token}",  # public Telegram URL
        )
    else:
        # ▶ Local dev mode (polling)
        print("WEBHOOK_URL not found -> running in POLLING mode (local test)")
        application.run_polling()


if __name__ == "__main__":
    main()
