import os
import re
import random
import subprocess
import datetime
import json
import shutil
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


def generate_todays_video():
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

    playlist_items = []
    
    # 2. Iterate through cards, check stability, and apply repeat rules
    for cid in card_ids:
        card = col.get_card(cid)
        
        stability_days = get_card_stability(card)
        repeat_count = get_repeat_count(stability_days)
        
        note = card.note()
        fields = dict(zip(note.keys(), note.values()))
        
        sentence_text = fields.get("Sentence", "").strip()
        word_text = fields.get("Word", "").strip()
        
        sentence_audio_field = fields.get("Sentence Audio", "")
        match = re.search(r"\[sound:(.*?)\]", sentence_audio_field)
        if match:
            filename = match.group(1)
            for _ in range(repeat_count):
                playlist_items.append({
                    "filename": filename,
                    "sentence": sentence_text
                })

    if not playlist_items:
        showInfo("No audio files found in the Sentence Audio fields for today's cards.")
        return

    # 3. Randomize the playlist
    random.shuffle(playlist_items)

    media_folder = col.media.dir()
    ffmpeg_executable = "/opt/homebrew/bin/ffmpeg"
    ffprobe_executable = "/opt/homebrew/bin/ffprobe"
    temp_dir = os.path.join(media_folder, "anki_video_temp")
    os.makedirs(temp_dir, exist_ok=True)

    # 4. Filter out items where audio files don't exist
    valid_items = []
    for item in playlist_items:
        full_path = os.path.join(media_folder, item["filename"])
        if os.path.exists(full_path):
            valid_items.append({
                "path": full_path,
                "sentence": item["sentence"]
            })

    if not valid_items:
        showInfo("No valid audio files found on disk.")
        shutil.rmtree(temp_dir)
        return

    print(f"Processing {len(valid_items)} audio items into master track...")

    # 5. Create master audio concat list and probe exact durations
    audio_list_file = os.path.join(temp_dir, "audio_concat_list.txt")
    with open(audio_list_file, "w", encoding="utf-8") as f:
        for item in valid_items:
            safe_path = item["path"].replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    master_audio = os.path.join(temp_dir, "master_audio.aac")
    
    # Concat all audio into a single clean AAC stream with async resampling
    audio_concat_cmd = [
        ffmpeg_executable, "-y",
        "-f", "concat", "-safe", "0",
        "-i", audio_list_file,
        "-c:a", "aac", "-b:a", "192k",
        "-ar", "44100", "-af", "aresample=async=1",
        master_audio
    ]
    subprocess.run(audio_concat_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 6. Build precise timeline intervals and escape text for FFmpeg drawtext
    current_time = 0.0
    timeline_filters = []

    font_path = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
    if not os.path.exists(font_path):
        font_path = "/System/Library/Fonts/Hiragino Sans GB.ttc"

    for idx, item in enumerate(valid_items):
        probe_cmd = [
            ffprobe_executable,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            item["path"]
        ]
        try:
            result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            duration = float(result.stdout.strip())
        except Exception:
            duration = 3.0  # Fallback duration if probe fails

        start_time = current_time
        end_time = current_time + duration
        current_time = end_time

        clean_sentence = re.sub(r'<[^>]+>', '', item["sentence"])
        safe_sentence = clean_sentence.replace("\\", "\\\\").replace("'", "").replace(":", "\\:").replace("%", "\\%")

        in_label = "[v_in]" if idx == 0 else f"[v{idx-1}]"
        out_label = f"[v{idx}]" if idx < len(valid_items) - 1 else "[v_out]"

        filter_str = (
            f"{in_label}drawtext=fontfile='{font_path}':text='{safe_sentence}':"
            f"fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2:"
            f"enable='between(t,{start_time},{end_time})'{out_label}"
        )
        timeline_filters.append(filter_str)

    full_filter_chain = f"[0:v]null[v_in];" + ";".join(timeline_filters)

    # 7. Set output paths for both Video and Audio in ~/Downloads
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    downloads_dir = os.path.expanduser("~/Downloads")
    output_mp4 = os.path.join(downloads_dir, f"{timestamp_str}_review_video.mp4")
    output_m4a = os.path.join(downloads_dir, f"{timestamp_str}_review_audio.m4a")

    print(f"Generating single-pass master video into {output_mp4}...")

    video_build_cmd = [
        ffmpeg_executable, "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s=1280x720:r=30:d={current_time}",
        "-i", master_audio,
        "-filter_complex", full_filter_chain,
        "-map", "[v_out]",
        "-map", "1:a",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-c:a", "copy",
        "-shortest",
        output_mp4
    ]

    try:
        subprocess.run(video_build_cmd, check=True)
        
        # Copy/Export the master audio track as a standalone .m4a file
        shutil.copy(master_audio, output_m4a)
        
        showInfo(f"Success! Created both files in your Downloads:\n\n1. Video:\n{output_mp4}\n\n2. Audio:\n{output_m4a}")
    except subprocess.CalledProcessError as e:
        showInfo(f"Error building media with ffmpeg: {e}")
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


# Add a menu item under Anki's "Tools" menu
action = QAction("Generate Randomized Review Video & Audio", mw)
qconnect(action.triggered, generate_todays_video)
mw.form.menuTools.addAction(action)
