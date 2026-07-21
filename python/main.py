# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0
import base64
import threading
import time
import os
import sys
import sqlite3
import cv2
import numpy as np
import requests
import tempfile
from datetime import datetime, UTC
from arduino.app_utils import App
from arduino.app_bricks.web_ui import WebUI
from arduino.app_bricks.audio_classification import AudioClassification
from arduino.app_peripherals.microphone import Microphone

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)


from bricks.kiosk_modal import KioskLauncher

ui = WebUI()

# Set to False to fall back to App Lab's built-in browser-launch behavior
# (i.e. "roll back" without deleting anything below).
USE_KIOSK_LAUNCHER = True

if USE_KIOSK_LAUNCHER:
    kiosk = KioskLauncher(url=ui.local_url)

# --- Camera ------------------------------------------------------------
CAMERA_INDEX = 0  # adjust if your USB camera doesn't show up at index 0


def open_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if cap.isOpened():
        return cap
    for idx in range(1, 4):
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            print(f"Camera found at index {idx} instead of {CAMERA_INDEX}; consider updating CAMERA_INDEX.")
            return cap
    return None


camera = open_camera()
if camera is None:
    print("Warning: could not open any camera at startup. Capture will fail until this is resolved.")

_capture_lock = threading.Lock()

# All actual camera.read() calls -- from the preview loop or from a
# capture -- go through this lock, since cv2.VideoCapture isn't safe to
# hit from two threads at once.
_camera_io_lock = threading.Lock()

# --- Live preview --------------------------------------------------
PREVIEW_FPS = 8
PREVIEW_JPEG_QUALITY = 60
PREVIEW_MAX_WIDTH = 480

_frame_lock = threading.Lock()
_latest_frame = None


def preview_loop():
    global _latest_frame
    interval = 1.0 / PREVIEW_FPS
    while True:
        if camera is None or not camera.isOpened():
            time.sleep(1)
            continue

        with _camera_io_lock:
            ret, frame = camera.read()
        if not ret:
            time.sleep(interval)
            continue

        with _frame_lock:
            _latest_frame = frame

        preview_frame = frame
        h, w = frame.shape[:2]
        if w > PREVIEW_MAX_WIDTH:
            scale = PREVIEW_MAX_WIDTH / w
            preview_frame = cv2.resize(frame, (PREVIEW_MAX_WIDTH, int(h * scale)))

        ok, buffer = cv2.imencode('.jpg', preview_frame, [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_JPEG_QUALITY])
        if ok:
            b64 = base64.b64encode(buffer).decode('utf-8')
            ui.send_message("frame", message={"jpeg": b64})

        time.sleep(interval)


threading.Thread(target=preview_loop, daemon=True).start()

# --- Local journal storage: SQLite -----------------------------------
# One row per field-journal entry: photo, sketch, and AI note all live
# together on the same entry, since they're saved as one unit.
JOURNAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal")
os.makedirs(JOURNAL_DIR, exist_ok=True)
DB_PATH = os.path.join(JOURNAL_DIR, "journal.db")

_db_lock = threading.Lock()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            photo BLOB,
            sketch BLOB,
            ai_note TEXT
        )
    """)
    # Grinnell-method structured fields (locality/weather/habitat), added
    # after the original schema -- migrate existing databases in place by
    # adding any missing columns, so entries captured before this update
    # aren't lost or require a fresh journal.db.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
    for column in ("locality", "weather", "habitat"):
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {column} TEXT")
    conn.commit()
    conn.close()


init_db()


def catalog_number(entry_id):
    """Grinnell-style accession number for an entry, e.g. 'BLM-0007'.
    Derived from the row id rather than stored as its own column, so it
    always stays in lockstep with the database and can never drift out
    of sync with it."""
    return f"BLM-{entry_id:04d}"

# Which entry the operator is currently working on -- capture starts a
# new one; sketch-save and ask-llm both act on this one until the next
# capture starts a fresh entry.
_state_lock = threading.Lock()
_current_entry_id = None


def capture_photo(conditions=None):
    """Called when the Web UI sends a 'capture' message. Takes a fresh
    frame directly from the camera, guarded by _camera_io_lock so it
    can't collide with the preview loop's own reads.

    conditions is an optional dict of Grinnell-method field metadata
    (locality/weather/habitat) typed in on the Capture tab -- these
    persist there across captures since conditions like weather rarely
    change entry-to-entry within one outing, so they're attached to
    whichever new entry is created here rather than re-typed each time."""
    global _current_entry_id
    conditions = conditions or {}

    if camera is None or not camera.isOpened():
        print("No camera available.")
        ui.send_message("status", message={"text": "Camera not available."})
        return

    if not _capture_lock.acquire(blocking=False):
        print("Capture already in progress; ignoring this press.")
        return

    try:
        with _camera_io_lock:
            ret, frame = camera.read()

        if not ret or frame is None:
            print("Could not read a frame from the camera.")
            ui.send_message("status", message={"text": "Camera warming up, try again shortly."})
            return

        ok, buffer = cv2.imencode('.jpg', frame)
        if not ok:
            print("Failed to encode captured frame.")
            ui.send_message("status", message={"text": "Failed to save photo."})
            return

        photo_bytes = buffer.tobytes()
        timestamp = datetime.now(UTC).isoformat()
        locality = conditions.get("locality") or None
        weather = conditions.get("weather") or None
        habitat = conditions.get("habitat") or None

        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute(
                "INSERT INTO entries (timestamp, photo, locality, weather, habitat) VALUES (?, ?, ?, ?, ?)",
                (timestamp, photo_bytes, locality, weather, habitat)
            )
            conn.commit()
            entry_id = cursor.lastrowid
            conn.close()

        with _state_lock:
            _current_entry_id = entry_id

        print(f"Photo saved as entry {catalog_number(entry_id)}.")
        ui.send_message("photo_saved", message={
            "id": entry_id,
            "catalog_no": catalog_number(entry_id),
            "timestamp": timestamp,
            "image": base64.b64encode(photo_bytes).decode("utf-8")
        })
    finally:
        _capture_lock.release()


def handle_capture_message(sid, data=None):
    conditions = {
        "locality": (data or {}).get("locality", "").strip(),
        "weather": (data or {}).get("weather", "").strip(),
        "habitat": (data or {}).get("habitat", "").strip(),
    }
    threading.Thread(target=capture_photo, args=(conditions,), daemon=True).start()


ui.on_message("capture", handle_capture_message)


def handle_sketch_message(sid, data):
    """Saves the sketch onto whichever entry it belongs to -- the entry
    named in the message if given, otherwise the entry currently being
    worked on."""
    try:
        image_b64 = data["image"]
        image_bytes = base64.b64decode(image_b64)

        with _state_lock:
            entry_id = data.get("entry_id") or _current_entry_id

        if entry_id is None:
            print("No current entry to attach a sketch to -- capture a photo first.")
            ui.send_message("status", message={"text": "Capture a photo before saving a sketch."})
            return

        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE entries SET sketch = ? WHERE id = ?", (image_bytes, entry_id))
            conn.commit()
            conn.close()

        print(f"Sketch saved to entry #{entry_id} ({len(image_bytes)} bytes).")
        ui.send_message("sketch_saved", message={"id": entry_id})
    except Exception as e:
        print(f"Failed to save sketch: {e}")
        ui.send_message("status", message={"text": "Failed to save sketch."})


ui.on_message("sketch", handle_sketch_message)

# --- Local LLM chat -----------------------------------------------------
# Text-only, fully offline (Qwen 3.5 0.8B, downloaded on-device). No
# image support -- the person describes what they're observing in their
# own words or asks a question, and the model responds conversationally.
# Each response can be appended into the current entry's field note.
#
# Confirmed against Arduino's own "Edge AI Assistant" reference app:
# it streams via chat_stream() (yielding one chunk at a time) rather
# than a single blocking chat() call, and with_memory() is called as a
# bare statement (mutates in place, no reassignment needed).
from arduino.app_bricks.llm import LargeLanguageModel

field_llm = LargeLanguageModel(
    system_prompt=(
        "You are a knowledgeable field naturalist assistant. The user is out in the "
        "field describing a plant, animal, or other observation in their own words, or "
        "asking you questions about one. Give concise, honest answers in a few sentences, "
        "suitable for pasting directly into a handwritten field journal. Say when you're "
        "uncertain rather than guessing confidently."
    )
)
field_llm.with_memory(10)


def handle_llm_chat_message(sid, data):
    question = (data or {}).get("message", "").strip()
    if not question:
        return
    threading.Thread(target=run_llm_chat, args=(question,), daemon=True).start()


def run_llm_chat(question):
    try:
        for chunk in field_llm.chat_stream(question):
            ui.send_message("llm_response_chunk", chunk)
        ui.send_message("llm_response_end", {})
    except Exception as e:
        print(f"Local LLM chat failed: {e}")
        ui.send_message("llm_error", {"error": str(e)})


ui.on_message("llm_chat", handle_llm_chat_message)


def handle_append_note_message(sid, data):
    entry_id = (data or {}).get("entry_id")
    text = (data or {}).get("text", "").strip()
    if entry_id is None or not text:
        return

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT ai_note FROM entries WHERE id = ?", (entry_id,)).fetchone()
        existing = row[0] if row and row[0] else ""
        updated = (existing + "\n\n" + text).strip() if existing else text
        conn.execute("UPDATE entries SET ai_note = ? WHERE id = ?", (updated, entry_id))
        conn.commit()
        conn.close()

    print(f"Appended to field note for entry #{entry_id}.")
    ui.send_message("field_note", message={
        "id": entry_id,
        "note": updated,
        "timestamp": datetime.now(UTC).isoformat()
    })


ui.on_message("append_note", handle_append_note_message)

# --- Saved Notes tab: browse past entries -------------------------------
# Nothing previously let the UI look back at old entries -- every message
# above only ever pushes the *current* one. The Notes tab needs a list
# (lightweight -- small thumbnail + note snippet, not full-size images)
# and a detail lookup (full photo/sketch/note) for whichever entry gets
# tapped.
THUMB_WIDTH = 96


def _make_thumbnail(photo_bytes):
    """Decodes a stored photo BLOB and re-encodes a small JPEG thumbnail
    for the entries list, so we're not shipping full-resolution photos
    over the socket just to render a 96px-wide row icon."""
    if not photo_bytes:
        return None
    try:
        arr = np.frombuffer(photo_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = THUMB_WIDTH / w
        thumb = cv2.resize(img, (THUMB_WIDTH, max(1, int(h * scale))))
        ok, buffer = cv2.imencode('.jpg', thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        return base64.b64encode(buffer).decode('utf-8') if ok else None
    except Exception as e:
        print(f"Thumbnail generation failed: {e}")
        return None


def handle_list_entries(sid, data=None):
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, timestamp, photo, ai_note, locality FROM entries ORDER BY id DESC"
        ).fetchall()
        conn.close()

    entries = [
        {
            "id": entry_id,
            "catalog_no": catalog_number(entry_id),
            "timestamp": timestamp,
            "note": ai_note or "",
            "locality": locality or "",
            "thumbnail": _make_thumbnail(photo),
        }
        for entry_id, timestamp, photo, ai_note, locality in rows
    ]
    ui.send_message("entries_list", message={"entries": entries})


ui.on_message("list_entries", handle_list_entries)


def handle_get_entry(sid, data):
    entry_id = (data or {}).get("entry_id")
    if entry_id is None:
        return

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id, timestamp, photo, sketch, ai_note, locality, weather, habitat FROM entries WHERE id = ?",
            (entry_id,)
        ).fetchone()
        conn.close()

    if row is None:
        ui.send_message("status", message={"text": f"Entry #{entry_id} not found."})
        return

    eid, timestamp, photo, sketch, ai_note, locality, weather, habitat = row
    ui.send_message("entry_detail", message={
        "id": eid,
        "catalog_no": catalog_number(eid),
        "timestamp": timestamp,
        "note": ai_note or "",
        "locality": locality or "",
        "weather": weather or "",
        "habitat": habitat or "",
        "photo": base64.b64encode(photo).decode("utf-8") if photo else None,
        "sketch": base64.b64encode(sketch).decode("utf-8") if sketch else None,
    })


ui.on_message("get_entry", handle_get_entry)


def handle_update_note(sid, data):
    """Full replace of an entry's note text and Grinnell-method field
    conditions (locality/weather/habitat) -- used by the Notes tab's
    single Edit/Save flow. (Different from append_note, which the Chat
    tab uses to tack streamed LLM answers onto the note in progress.)"""
    entry_id = (data or {}).get("entry_id")
    if entry_id is None:
        return
    text = (data or {}).get("text", "").strip()
    locality = (data or {}).get("locality", "").strip()
    weather = (data or {}).get("weather", "").strip()
    habitat = (data or {}).get("habitat", "").strip()

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE entries SET ai_note = ?, locality = ?, weather = ?, habitat = ? WHERE id = ?",
            (text, locality or None, weather or None, habitat or None, entry_id)
        )
        conn.commit()
        conn.close()

    print(f"Note/fields for entry {catalog_number(entry_id)} updated by hand.")
    ui.send_message("note_updated", message={
        "id": entry_id,
        "note": text,
        "locality": locality,
        "weather": weather,
        "habitat": habitat,
    })


ui.on_message("update_note", handle_update_note)


def handle_delete_entry(sid, data):
    entry_id = (data or {}).get("entry_id")
    if entry_id is None:
        return

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()

    with _state_lock:
        global _current_entry_id
        if _current_entry_id == entry_id:
            _current_entry_id = None

    print(f"Deleted entry #{entry_id}.")
    ui.send_message("entry_deleted", message={"id": entry_id})


ui.on_message("delete_entry", handle_delete_entry)

# --- Cloud sync: push one finished entry to the companion web app ------
# A separate, deliberate per-entry action -- nothing leaves the board
# unless you tap Sync on that specific note, keeping the offline-first
# story honest. This posts to field-notes.netlify.app; since I don't
# have your actual ingest route/payload contract, treat CLOUD_SYNC_URL
# and the payload shape below as a starting guess to confirm against
# whatever your Netlify app actually expects.
CLOUD_SYNC_URL = "https://field-notes.netlify.app/api/entries"
CLOUD_SYNC_TIMEOUT = 8


def sync_entry(entry_id):
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id, timestamp, photo, sketch, ai_note, locality, weather, habitat FROM entries WHERE id = ?",
            (entry_id,)
        ).fetchone()
        conn.close()

    if row is None:
        ui.send_message("sync_result", message={
            "id": entry_id, "success": False, "error": "Entry not found."
        })
        return

    eid, timestamp, photo, sketch, ai_note, locality, weather, habitat = row
    payload = {
        "id": eid,
        "catalog_no": catalog_number(eid),
        "timestamp": timestamp,
        "note": ai_note or "",
        "locality": locality or "",
        "weather": weather or "",
        "habitat": habitat or "",
        "photo": base64.b64encode(photo).decode("utf-8") if photo else None,
        "sketch": base64.b64encode(sketch).decode("utf-8") if sketch else None,
    }

    try:
        response = requests.post(CLOUD_SYNC_URL, json=payload, timeout=CLOUD_SYNC_TIMEOUT)
        if 200 <= response.status_code < 300:
            print(f"Entry #{eid} synced to {CLOUD_SYNC_URL}.")
            ui.send_message("sync_result", message={"id": eid, "success": True})
        else:
            print(f"Sync failed for entry #{eid}: HTTP {response.status_code}")
            ui.send_message("sync_result", message={
                "id": eid, "success": False,
                "error": f"Server responded with HTTP {response.status_code}"
            })
    except requests.exceptions.RequestException as e:
        # No Wi-Fi / unreachable host -- this only reports whether the
        # push worked; the entry itself stays saved locally either way.
        print(f"Sync failed for entry #{eid}: {e}")
        ui.send_message("sync_result", message={
            "id": eid, "success": False,
            "error": "No connection -- entry is still saved locally."
        })


def handle_sync_entry(sid, data):
    entry_id = (data or {}).get("entry_id")
    if entry_id is None:
        return
    threading.Thread(target=sync_entry, args=(entry_id,), daemon=True).start()


ui.on_message("sync_entry", handle_sync_entry)

# --- Birdsong identification: USB microphone + custom Edge Impulse -----
# model ------------------------------------------------------------------
# Unlike Motion Detection, this needs no MCU/sketch/Bridge involvement --
# a USB microphone is read directly on the Linux side, same as the USB
# camera, so there's no extra hardware purchase beyond a USB mic.
#
# Model: "Bird Calls" (studio.edgeimpulse.com/studio/1062343). The old
# "background/moudov" merged class has since been split in Edge Impulse
# Studio into a clean "moudov" (Mourning Dove) class plus a separate
# "background"/"noise" class, and two more species ("blujay", "norcar")
# were added -- "noise" is a real trained class (background sound), not
# a generic rejection bucket, so a confident "noise" result is handled
# the same as no match at all (see below) rather than shown as a bird ID.
#
# This uses AudioClassification.classify_from_file() -- a deliberate
# "record N seconds, then classify" one-shot, not the Brick's continuous
# on_detect()/start() streaming mode. That matches the app's existing
# ritual: point, press, wait a moment, get a result -- rather than
# always-on listening.
BIRDSONG_LISTEN_SECONDS = 5
BIRDSONG_SAMPLE_RATE = Microphone.RATE_16K  # matches the model's 16kHz training data
BIRDSONG_CONFIDENCE = 0.6  # bioacoustic models often need a lower bar than the Brick's 0.8 default -- tune on-device

# Whichever class names count as "not a bird" -- checked against
# whatever the active model's class_name comes back as. Now that
# "background/moudov" has been split apart in Edge Impulse Studio,
# Mourning Dove calls classify cleanly as "moudov" and no longer get
# discarded as noise.
BIRDSONG_NON_BIRD_CLASSES = {"noise", "background"}

# Raw class names aren't display-friendly ("amecro") -- map to a
# human-readable label for the note text and UI. Falls back to the raw
# class name for anything not in this map, so a retrained/expanded model
# (more species added later) doesn't silently break.
#
# Current "Bird Calls" (studio.edgeimpulse.com/studio/1062343) class
# list: standard bird-banding codes -- amecro, amepip, amerob, moudov,
# rethaw, rocpig, blujay, norcar -- plus background/noise handled above.
BIRDSONG_LABELS = {
    "amecro": "American Crow",
    "amepip": "American Pipit",
    "amerob": "American Robin",
    "moudov": "Mourning Dove",
    "rethaw": "Red-tailed Hawk",
    "rocpig": "Rock Pigeon",
    "blujay": "Blue Jay",
    "norcar": "Northern Cardinal",
}

AUDIO_SCRATCH_DIR = os.path.join(JOURNAL_DIR, "audio_scratch")


def _ensure_audio_scratch_dir():
    """Recreates AUDIO_SCRATCH_DIR right before each use instead of trusting
    the one-time makedirs() call at import time. On-device this directory
    was observed missing at write time even though it's created at import --
    something between App Lab's process/container lifecycle and this app's
    startup isn't preserving it. Falls back to the system temp dir (always
    writable) if the preferred path still can't be created, so a listen
    never fails outright over a scratch-file location."""
    try:
        os.makedirs(AUDIO_SCRATCH_DIR, exist_ok=True)
        return AUDIO_SCRATCH_DIR
    except Exception as e:
        print(f"Could not create {AUDIO_SCRATCH_DIR} ({e}); falling back to system temp dir.")
        fallback = os.path.join(tempfile.gettempdir(), "bloom_audio_scratch")
        os.makedirs(fallback, exist_ok=True)
        return fallback


# Instantiated once at startup (even though only the static
# classify_from_file() is used below) so a missing/misconfigured model
# fails loudly here, at boot, rather than silently on first use.
bird_classifier = AudioClassification(confidence=BIRDSONG_CONFIDENCE)


def listen_for_birdsong():
    ui.send_message("status", message={"text": f"Listening for birdsong ({BIRDSONG_LISTEN_SECONDS}s)..."})

    try:
        wav_bytes = Microphone.record_wav(
            duration=BIRDSONG_LISTEN_SECONDS,
            sample_rate=BIRDSONG_SAMPLE_RATE,
            channels=Microphone.CHANNELS_MONO,
            format=np.int16,
            device=Microphone.USB_MIC_1,
        )
    except Exception as e:
        print(f"Failed to record from microphone: {e}")
        ui.send_message("bird_result", message={"match": False, "error": f"Microphone error: {e}"})
        return

    scratch_dir = _ensure_audio_scratch_dir()
    last_listen_wav_path = os.path.join(scratch_dir, "last_listen.wav")

    try:
        with open(last_listen_wav_path, "wb") as f:
            f.write(wav_bytes.tobytes())
    except Exception as e:
        print(f"Failed to write recorded audio to disk: {e}")
        ui.send_message("bird_result", message={"match": False, "error": f"Could not save recording: {e}"})
        return

    try:
        # Ask for the raw top prediction (confidence=0.0) instead of letting
        # the Brick apply BIRDSONG_CONFIDENCE itself -- if the real class is
        # below our threshold, the Brick would just hand back None, and we'd
        # have no way to tell "the model guessed the right species at 0.4"
        # apart from "the model got nothing at all". Applying the threshold
        # ourselves below means every listen logs what the model actually
        # heard, which is what you need on-device to tune BIRDSONG_CONFIDENCE
        # or notice a labeling/model problem instead of a mic/volume one.
        result = AudioClassification.classify_from_file(last_listen_wav_path, confidence=0.0)
    except Exception as e:
        print(f"Birdsong classification failed: {e}")
        ui.send_message("bird_result", message={"match": False, "error": f"Classification error: {e}"})
        return

    if result is None:
        print("No bird call detected: model returned no prediction at all for this clip.")
        ui.send_message("bird_result", message={"match": False})
        return

    # Normalize confidence to a 0-1 float in exactly one place. On-device
    # this Brick was observed returning confidence on a 0-100 scale (e.g.
    # 62.85) rather than 0-1 -- the frontend already multiplies by 100 to
    # display a percentage, so passing a 0-100 value through unchanged
    # doubled up into readings like "6285% confidence". Any value over 1
    # is treated as already-a-percentage and divided down.
    raw_confidence = result["confidence"]
    confidence = raw_confidence / 100.0 if raw_confidence > 1 else raw_confidence

    print(f"Raw classification: {result['class_name']} ({confidence:.2f})")

    if confidence < BIRDSONG_CONFIDENCE:
        print(f"Below confidence threshold ({BIRDSONG_CONFIDENCE}) -- treating as no match.")
        ui.send_message("bird_result", message={"match": False})
        return

    if result["class_name"] in BIRDSONG_NON_BIRD_CLASSES:
        print(f"Classified as non-bird class '{result['class_name']}' -- treating as no match.")
        ui.send_message("bird_result", message={"match": False})
        return

    label = BIRDSONG_LABELS.get(result["class_name"], result["class_name"])
    print(f"Birdsong match: {label} ({confidence:.2f})")
    ui.send_message("bird_result", message={
        "match": True,
        "class_name": label,
        "confidence": confidence,
    })


def handle_listen_for_birdsong(sid, data=None):
    threading.Thread(target=listen_for_birdsong, daemon=True).start()


ui.on_message("listen_for_birdsong", handle_listen_for_birdsong)

App.run()