import os
import re
from pathlib import Path
from datetime import datetime

import pdfplumber
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import asyncio

PDF_FOLDER = Path("pdfs")  # folder containing all your PDFs


def parse_time(text: str) -> str:
    """
    Extracts: Slot B (11:30 AM - 01:00 PM)
    from: 'Date: 05-11-2025 Slot: B (11:30 AM - 01:00 PM)'
    """
    m = re.search(r"Slot:\s*([A-Z])\s*\(([^)]+)\)", text)
    if not m:
        return ""
    return f"Slot {m.group(1)} ({m.group(2)})"


def parse_date(text: str) -> str:
    """
    Extracts date and converts to: 05-11-2025 (Wednesday)

    Looks for: Date: 05-11-2025
               or Date: 05/11/2025
    """
    m = re.search(r"Date:\s*([0-9]{2}[-/][0-9]{2}[-/][0-9]{4})", text)
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
            time_str = ""
            date_str = ""
            course_name = ""
            course_id = ""

            num_pages = len(pdf.pages)

            # use index so we can also look at next page (for split sections)
            for page_index in range(num_pages):
                page = pdf.pages[page_index]
                text = page.extract_text() or ""

                # date once per file
                if "Date:" in text and not date_str:
                    date_str = parse_date(text)

                # time once per file (slot)
                if "Slot:" in text and not time_str:
                    time_str = parse_time(text)

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

                            # ❌ skip grand total line like "Total 1506"
                            if room.lower() == "total":
                                continue

                            rooms.append((room, seats))

                if rooms and teacher:
                    results.append({
                        "date": date_str,
                        "time": time_str,
                        "section": section_code,
                        "teacher": teacher,
                        "rooms": rooms,
                        "total": total_seats,
                        "course_name": course_name,
                        "course_id": course_id,
                    })
                    # assume one occurrence of this section per file
                    break

    return results


def format_section_infos(section_code: str) -> str:
    infos = extract_all_section_infos(PDF_FOLDER, section_code)

    if not infos:
        return f"❌ No occurrences of section `{section_code}` found."

    blocks = []

    for info in infos:
        block = []
        block.append(f"=========== 📚 SECTION `{info['section']}` ===========")
        if info["date"]:
            block.append(f"📅 Date: {info['date']}")
        else:
            block.append("📅 Date: (not found)")
        block.append(f"⏰ {info['time']}\n")

        block.append(f"📘 Course: {info['course_name']} ({info['course_id']})")
        block.append(f"👨‍🏫 Teacher: {info['teacher']}\n")

        block.append("🏫 Rooms & Seats:")
        for room, seats in info["rooms"]:
            block.append(f"- Room {room} — {seats}")

        block.append(f"\n🧮 Total Seats: {info['total']}")
        block.append("======================================")

        blocks.append("\n".join(block))

    return "\n\n".join(blocks)


# ===================== TELEGRAM BOT PART ===================== #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hi! Send me your *section code*.\n\n"
        "Example:\n"
        "`66_A`\n"
        "`69_K`\n"
        "`64_B`\n\n"
        "I'll search all PDFs in the `pdfs` folder and show you the exam info."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    section_input = (update.message.text or "").strip().upper()

    # basic validation: must look like NN_X
    if not re.fullmatch(r"\d{2}_[A-Z]", section_input):
        await update.message.reply_text(
            "⚠️ Please send a valid section code like `66_A` or `69_K`.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("🔍 Searching, please wait...")

    # run the heavy PDF work in a thread so bot doesn't freeze
    result_text = await asyncio.to_thread(format_section_infos, section_input)

    await update.message.reply_text(result_text, parse_mode="Markdown")


def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise RuntimeError("BOT_TOKEN not found in .env file")

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_section))

    print("Bot is running...")  # for console log
    application.run_polling()


if __name__ == "__main__":
    main()
