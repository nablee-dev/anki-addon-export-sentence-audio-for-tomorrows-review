import os
import re
import random
import subprocess
import datetime
import json
import shutil
import html
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


def format_duration_lrc(seconds):
    """
    Returns a standard LRC timestamp format [mm:ss.xx] (minutes:seconds.hundredths).
    """
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    # If total duration exceeds an hour, include hours as [hh:mm:ss.xx]
    if hours > 0:
        total_mins = int(hours * 60 + mins)
        return f"[{total_mins:02d}:{secs:05.2f}]"
    return f"[{int(mins):02d}:{secs:05.2f}]"


def generate_todays_audio_lrc():
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

    print(f"Found {len(review_card_ids)} reviews for tomorrow and {len(tomorrow_new_card_ids)} new cards.")

    if not review_card_ids and not tomorrow_new_card_ids:
        showInfo("No cards found for tomorrow.")
        return

    media_folder = col.media.dir()
    ffmpeg_executable = "/opt/homebrew/bin/ffmpeg"
    ffprobe_executable = "/opt/homebrew/bin/ffprobe"
    temp_dir = os.path.join(media_folder, "anki_audio_temp")
    os.makedirs(temp_dir, exist_ok=True)

    # Helper function to extract audio items from a list of card IDs
    def extract_audio_items(card_ids_list):
        items = []
        for cid in card_ids_list:
            card = col.get_card(cid)
            stability_days = get_card_stability(card)
            repeat_count = get_repeat_count(stability_days)
            
            note = card.note()
            fields = dict(zip(note.keys(), note.values()))
            
            sentence_text = fields.get("Sentence", "").strip()
            sentence_audio_field = fields.get("Sentence Audio", "")
            
            match = re.search(r"\[sound:(.*?)\]", sentence_audio_field)
            if match:
                filename = match.group(1)
                full_path = os.path.join(media_folder, filename)
                if os.path.exists(full_path):
                    # Sequential repetition for low stability cards
                    for _ in range(repeat_count):
                        items.append({
                            "path": full_path,
                            "sentence": sentence_text
                        })
        return items

    # 2. Build separate playlists for reviews and new cards
    review_playlist = extract_audio_items(review_card_ids)
    new_playlist = extract_audio_items(tomorrow_new_card_ids)

    if not review_playlist and not new_playlist:
        showInfo("No valid audio files found on disk for today's cards.")
        shutil.rmtree(temp_dir)
        return

    # Create silence assets using ffmpeg
    silence_1s_path = os.path.join(temp_dir, "silence_1s.m4a")
    silence_5s_path = os.path.join(temp_dir, "silence_5s.m4a")

    subprocess.run([
        ffmpeg_executable, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "1", "-c:a", "aac", "-b:a", "192k", silence_1s_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run([
        ffmpeg_executable, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "5", "-c:a", "aac", "-b:a", "192k", silence_5s_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 3. Assemble sentence blocks and merge trailing silence into each sentence file
    blocks = []

    def prepare_blocks(segment_items):
        for i, item in enumerate(segment_items):
            if i < len(segment_items) - 1:
                trailing = silence_1s_path
            else:
                trailing = silence_5s_path
            
            blocks.append({
                "path": item["path"],
                "sentence": item["sentence"],
                "trailing_silence": trailing
            })

    has_reviews = bool(review_playlist)
    has_new = bool(new_playlist)

    if has_reviews:
        prepare_blocks(review_playlist)

    if has_new:
        prepare_blocks(new_playlist)

    playlist_sequence = []

    # Optional initial 5s silence if there are no reviews but we have new cards
    if not has_reviews and has_new:
        playlist_sequence.append({
            "path": silence_5s_path,
            "is_sentence": False
        })

    print(f"Merging sentence audio with trailing silence for {len(blocks)} items...")

    for idx, block in enumerate(blocks):
        combined_path = os.path.join(temp_dir, f"combined_{idx}.m4a")
        subprocess.run([
            ffmpeg_executable, "-y",
            "-i", block["path"],
            "-i", block["trailing_silence"],
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[outa]",
            "-map", "[outa]",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100",
            combined_path
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        playlist_sequence.append({
            "path": combined_path,
            "sentence": block["sentence"],
            "is_sentence": True
        })

    print(f"Processing master track with {len(playlist_sequence)} audio segments...")

    # 4. Use FFmpeg filter_complex to concatenate all combined audio inputs reliably
    master_audio = os.path.join(temp_dir, "master_audio.m4a")
    
    ffmpeg_cmd = [ffmpeg_executable, "-y"]
    for seq_item in playlist_sequence:
        ffmpeg_cmd.extend(["-i", seq_item["path"]])

    filter_parts = [f"[{i}:a]" for i in range(len(playlist_sequence))]
    filter_str = "".join(filter_parts) + f"concat=n={len(playlist_sequence)}:v=0:a=1[outa]"

    ffmpeg_cmd.extend([
        "-filter_complex", filter_str,
        "-map", "[outa]",
        "-c:a", "aac", "-b:a", "192k",
        "-ar", "44100",
        master_audio
    ])
    subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 5. Probe durations and build LRC timestamps
    current_time = 0.0
    lrc_lines = []

    # Optional LRC Metadata Headers
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    lrc_lines.append(f"[ti:Anki Daily Review - {timestamp_str}]")
    lrc_lines.append("[ar:Anki FSRS]")
    lrc_lines.append("[au:Generated by Anki Add-on]")
    lrc_lines.append("")

    for seq_item in playlist_sequence:
        probe_cmd = [
            ffprobe_executable,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            seq_item["path"]
        ]
        try:
            result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            duration = float(result.stdout.strip())
        except Exception:
            duration = 1.0  # Fallback duration

        start_time = current_time
        end_time = current_time + duration
        current_time = end_time

        if seq_item["is_sentence"]:
            clean_sentence = re.sub(r'<[^>]+>', '', seq_item["sentence"]).strip()
            timestamp_tag = format_duration_lrc(start_time)
            lrc_lines.append(f"{timestamp_tag}{clean_sentence}")
        else:
            # Standalone silence advances the timeline silently
            pass

    # 6. Save final files to ~/Downloads
    downloads_dir = os.path.expanduser("~/Downloads")
    output_audio = os.path.join(downloads_dir, f"{timestamp_str}_review.m4a")
    output_lrc = os.path.join(downloads_dir, f"{timestamp_str}_review.lrc")

    try:
        # Copy master audio to Downloads
        shutil.copy(master_audio, output_audio)

        # Write LRC file to Downloads
        with open(output_lrc, "w", encoding="utf-8") as f:
            f.write("\n".join(lrc_lines))

        showInfo(f"Success! Created Audio + LRC files in your Downloads:\n\n• {output_audio}\n• {output_lrc}")
    except Exception as e:
        showInfo(f"Error saving files to Downloads: {e}")
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


# Add a menu item under Anki's "Tools" menu
action = QAction("Generate Audio + LRC Review Files", mw)
qconnect(action.triggered, generate_todays_audio_lrc)
mw.form.menuTools.addAction(action)
