import os
import re
import random
import subprocess
import datetime
import json
import shutil
import zipfile
import html
import uuid
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


def format_duration_smil(seconds):
    """
    Returns a valid SMIL3 clock value with the 's' suffix (e.g., '12.345s').
    """
    return f"{seconds:.3f}s"


def format_duration_opf(seconds):
    """
    Returns a valid SMIL3 clock value format for OPF metadata (e.g., 'HH:MM:SS.mmm' or 'MM:SS.mmm').
    """
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{int(hours)}:{int(mins):02d}:{secs:06.3f}"
    elif mins > 0:
        return f"{int(mins):02d}:{secs:06.3f}"
    else:
        return f"00:{secs:06.3f}"


def generate_todays_epub_overlay():
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
    temp_dir = os.path.join(media_folder, "anki_epub_temp")
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

    def prepare_blocks(segment_items, is_last_segment):
        for i, item in enumerate(segment_items):
            if i < len(segment_items) - 1:
                trailing = silence_1s_path
            else:
                # Last item in segment gets a 5s transition or final silence
                trailing = silence_5s_path
            
            blocks.append({
                "path": item["path"],
                "sentence": item["sentence"],
                "trailing_silence": trailing
            })

    has_reviews = bool(review_playlist)
    has_new = bool(new_playlist)

    if has_reviews:
        prepare_blocks(review_playlist, is_last_segment=not has_new)

    if has_new:
        prepare_blocks(new_playlist, is_last_segment=True)

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
        # Concatenate sentence audio + trailing silence into one file so the highlight stays active during the pause
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

    # 5. Probe durations and build XHTML + SMIL timelines cleanly
    current_time = 0.0
    xhtml_paragraphs = []
    smil_paras = []
    sentence_idx = 0

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
            clean_sentence = re.sub(r'<[^>]+>', '', seq_item["sentence"])
            escaped_sentence = html.escape(clean_sentence)
            p_id = f"s{sentence_idx}"
            sentence_idx += 1

            xhtml_paragraphs.append(f'    <p id="{p_id}">{escaped_sentence}</p>')
            smil_paras.append(
                f'    <par id="p{sentence_idx}">\n'
                f'      <text src="text.xhtml#{p_id}"/>\n'
                f'      <audio src="master_audio.m4a" clipBegin="{format_duration_smil(start_time)}" clipEnd="{format_duration_smil(end_time)}"/>\n'
                f'    </par>'
            )
        else:
            # Standalone leading silence items advance the timeline without text
            pass

    total_duration_opf_str = format_duration_opf(current_time)

    # 6. Assemble EPUB components
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    downloads_dir = os.path.expanduser("~/Downloads")
    output_epub = os.path.join(downloads_dir, f"{timestamp_str}_review_overlay.epub")

    epub_build_dir = os.path.join(temp_dir, "epub_root")
    os.makedirs(os.path.join(epub_build_dir, "META-INF"), exist_ok=True)
    os.makedirs(os.path.join(epub_build_dir, "OEBPS"), exist_ok=True)

    with open(os.path.join(epub_build_dir, "mimetype"), "w", encoding="utf-8") as f:
        f.write("application/epub+zip")

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        '  </rootfiles>\n'
        '</container>'
    )
    with open(os.path.join(epub_build_dir, "META-INF", "container.xml"), "w", encoding="utf-8") as f:
        f.write(container_xml)

    xhtml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="ja">\n'
        '<head>\n'
        '  <meta charset="utf-8"/>\n'
        '  <title>Daily Review Sentences</title>\n'
        '  <style>\n'
        '    body { font-family: sans-serif; padding: 2em; line-height: 1.8; font-size: 1.4em; }\n'
        '    p { margin-bottom: 1.5em; }\n'
        '    .-epub-media-overlay-active { background-color: #ffd700; color: #000; }\n'
        '  </style>\n'
        '</head>\n'
        '<body>\n'
        '  <h1>Daily Review</h1>\n'
        + "\n".join(xhtml_paragraphs) + "\n"
        '</body>\n'
        '</html>'
    )
    with open(os.path.join(epub_build_dir, "OEBPS", "text.xhtml"), "w", encoding="utf-8") as f:
        f.write(xhtml_content)

    smil_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '  <body>\n'
        '    <seq id="review_seq" epub:textref="text.xhtml" epub:type="bodymatter">\n'
        + "\n".join(smil_paras) + "\n"
        '    </seq>\n'
        '  </body>\n'
        '</smil>'
    )
    with open(os.path.join(epub_build_dir, "OEBPS", "overlay.smil"), "w", encoding="utf-8") as f:
        f.write(smil_content)

    nav_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ja">\n'
        '<head><meta charset="utf-8"/><title>Navigation</title></head>\n'
        '<body>\n'
        '  <nav epub:type="toc" id="toc">\n'
        '    <h1>Table of Contents</h1>\n'
        '    <ol>\n'
        '      <li><a href="text.xhtml">Review Sentences</a></li>\n'
        '    </ol>\n'
        '  </nav>\n'
        '</body>\n'
        '</html>'
    )
    with open(os.path.join(epub_build_dir, "OEBPS", "nav.xhtml"), "w", encoding="utf-8") as f:
        f.write(nav_content)

    unique_uuid = str(uuid.uuid4())
    datetime_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    opf_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="uid">urn:uuid:{unique_uuid}</dc:identifier>\n'
        f'    <dc:title>Anki Daily Review - {timestamp_str}</dc:title>\n'
        '    <dc:language>ja</dc:language>\n'
        f'    <meta property="dcterms:modified">{datetime_iso}</meta>\n'
        f'    <meta property="media:duration" refines="#overlay">{total_duration_opf_str}</meta>\n'
        f'    <meta property="media:duration">{total_duration_opf_str}</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>\n'
        '    <item id="text" href="text.xhtml" media-type="application/xhtml+xml" media-overlay="overlay"/>\n'
        '    <item id="overlay" href="overlay.smil" media-type="application/smil+xml"/>\n'
        '    <item id="audio" href="master_audio.m4a" media-type="audio/mp4"/>\n'
        '  </manifest>\n'
        '  <spine>\n'
        '    <itemref idref="text"/>\n'
        '  </spine>\n'
        '</package>'
    )
    with open(os.path.join(epub_build_dir, "OEBPS", "content.opf"), "w", encoding="utf-8") as f:
        f.write(opf_content)

    shutil.copy(master_audio, os.path.join(epub_build_dir, "OEBPS", "master_audio.m4a"))

    # 7. Zip into standard .epub container
    print(f"Packaging EPUB file into {output_epub}...")
    try:
        with zipfile.ZipFile(output_epub, "w") as epub_zip:
            mimetype_path = os.path.join(epub_build_dir, "mimetype")
            epub_zip.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)

            for root, dirs, files in os.walk(epub_build_dir):
                for file in files:
                    if file == "mimetype":
                        continue
                    full_file_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_file_path, epub_build_dir)
                    epub_zip.write(full_file_path, arcname, compress_type=zipfile.ZIP_DEFLATED)

        showInfo(f"Success! Created EPUB3 file with synced audio and clean text in your Downloads:\n\n{output_epub}")
    except Exception as e:
        showInfo(f"Error packaging EPUB file: {e}")
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


# Add a menu item under Anki's "Tools" menu
action = QAction("Generate EPUB3 Review with Media Overlays", mw)
qconnect(action.triggered, generate_todays_epub_overlay)
mw.form.menuTools.addAction(action)
