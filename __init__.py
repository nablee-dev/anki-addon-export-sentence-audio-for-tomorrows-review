import os
import re
import random
import subprocess
import datetime
import json
from aqt import mw
from aqt.qt import QAction, qconnect
from aqt.utils import showInfo


def get_card_stability(card):
    """
    Safely extracts FSRS stability (in days) from the card's internal database data field.
    """
    try:
        card_data = getattr(card, "data", None)
        if card_data:
            if isinstance(card_data, str) and card_data.startswith("{"):
                data_dict = json.loads(card_data)
                if "s" in data_dict:
                    return float(data_dict["s"])
            elif isinstance(card_data, bytes):
                data_dict = json.loads(card_data.decode("utf-8"))
                if "s" in data_dict:
                    return float(data_dict["s"])

        result = mw.col.db.scalar("SELECT data FROM cards WHERE id = ?", card.id)
        if result:
            if isinstance(result, str) and result.startswith("{"):
                data_dict = json.loads(result)
                if "s" in data_dict:
                    return float(data_dict["s"])
            elif isinstance(result, bytes):
                data_dict = json.loads(result.decode("utf-8"))
                if "s" in data_dict:
                    return float(data_dict["s"])
    except Exception as e:
        print(f"Error reading stability for card {card.id}: {e}")
        
    return None


def get_repeat_count(stability_days):
    """
    Applies your custom repetition rules based on stability in hours/days.
    """
    if stability_days is None:
        return 1

    stability_hours = stability_days * 24.0

    if stability_hours < 0.5:      # < 30 minutes
        return 10
    elif stability_hours < 1.0:    # < 1 hour
        return 8
    elif stability_hours < 6.0:    # < 6 hours
        return 6
    elif stability_hours < 12.0:   # < 12 hours
        return 4
    elif stability_hours < 24.0:   # < 24 hours
        return 2
    else:                          # >= 24 hours
        return 1


def generate_todays_audio():
    if not mw.col:
        showInfo("Anki collection is not loaded.")
        return

    col = mw.col
    print("Finding cards directly via Anki backend...")

    deck_filter = 'deck:"Kaishi 1.5k"'

    # 1a. Find review cards scheduled for tomorrow (prop:due=1)
    review_query = f"{deck_filter} prop:due=1".strip()
    review_card_ids = list(col.find_cards(review_query))

    # 1b. Find new cards and slice the first 10
    new_query = f"{deck_filter} is:new".strip()
    all_new_card_ids = list(col.find_cards(new_query))
    tomorrow_new_card_ids = all_new_card_ids[:10]

    card_ids = review_card_ids + tomorrow_new_card_ids

    print(f"Found {len(review_card_ids)} reviews for tomorrow and {len(tomorrow_new_card_ids)} new cards ({len(card_ids)} total).")

    if not card_ids:
        showInfo("No cards found for tomorrow.")
        return

    audio_entries = []
    
    # 2. Iterate through cards, check stability, and apply repeat rules
    for cid in card_ids:
        card = col.get_card(cid)
        
        stability_days = get_card_stability(card)
        repeat_count = get_repeat_count(stability_days)
        
        note = card.note()
        fields = dict(zip(note.keys(), note.values()))
        
        sentence_text = fields.get("Sentence", "N/A")
        word_text = fields.get("Word", "N/A")
        
        print(f"[{word_text}] '{sentence_text}' -> Stability: {stability_days}, Repeats: {repeat_count}")
        
        sentence_audio_field = fields.get("Sentence Audio", "")
        match = re.search(r"\[sound:(.*?)\]", sentence_audio_field)
        if match:
            filename = match.group(1)
            for _ in range(repeat_count):
                audio_entries.append(filename)

    print(f"Total audio tracks after applying low-stability repetition rules: {len(audio_entries)}")

    if not audio_entries:
        showInfo("No audio files found in the Sentence Audio fields for today's cards.")
        return

    # 3. Randomize the playlist so repetitions are non-sequential
    random.shuffle(audio_entries)

    # 4. Get Anki media folder path
    media_folder = col.media.dir()

    # 4b. Generate a temporary 0.5-second silent MP3 file
    ffmpeg_executable = "/opt/homebrew/bin/ffmpeg"
    silence_path = os.path.join(media_folder, "temp_silence_0.5s.mp3")
    silence_cmd = [
        ffmpeg_executable,
        "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "0.5",
        "-q:a", "9",
        silence_path
    ]
    try:
        subprocess.run(silence_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not generate silence file: {e}")

    # 5. Create a text file list for ffmpeg concatenation with silence inserted between tracks
    list_file_path = os.path.join(media_folder, "ffmpeg_list.txt")
    valid_files_count = 0

    with open(list_file_path, "w", encoding="utf-8") as f:
        for filename in audio_entries:
            full_path = os.path.join(media_folder, filename)
            if os.path.exists(full_path):
                safe_path = full_path.replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
                valid_files_count += 1
                
                # Insert 0.5s silence after each track if available
                if os.path.exists(silence_path):
                    safe_silence_path = silence_path.replace("'", "'\\''")
                    f.write(f"file '{safe_silence_path}'\n")
            else:
                print(f"Warning: Audio file not found on disk: {filename}")

    if valid_files_count == 0:
        showInfo("No valid audio files found on disk to merge.")
        if os.path.exists(list_file_path):
            os.remove(list_file_path)
        if os.path.exists(silence_path):
            os.remove(silence_path)
        return

    # 6. Set output to ~/Downloads with date and precise timestamp
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    downloads_dir = os.path.expanduser("~/Downloads")
    output_mp3 = os.path.join(downloads_dir, f"{timestamp_str}_sentence_audio.mp3")
    
    print(f"Stitching {valid_files_count} audio files with 0.5s pauses into {output_mp3}...")

    ffmpeg_cmd = [
        ffmpeg_executable,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file_path,
        "-c:a",
        "libmp3lame",
        output_mp3,
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True)
        showInfo(f"Success! Created timestamped audio file with pauses in your Downloads:\n{output_mp3}")
    except subprocess.CalledProcessError as e:
        showInfo(f"Error running ffmpeg: {e}")
    finally:
        # Clean up temp files
        if os.path.exists(list_file_path):
            os.remove(list_file_path)
        if os.path.exists(silence_path):
            os.remove(silence_path)


# Add a menu item under Anki's "Tools" menu
action = QAction("Generate Randomized Low-Stability Audio", mw)
qconnect(action.triggered, generate_todays_audio)
mw.form.menuTools.addAction(action)
