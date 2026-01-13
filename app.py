from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import os
import subprocess
import uuid
import requests
from pathlib import Path

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# Use /tmp for Railway/serverless
UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")

def ensure_dirs():
    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

ensure_dirs()


@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ElevenLabs config
ELEVENLABS_API_KEY = "sk_15030702d8a0c524641ab32fa7269048a83dfdabfbdec8cf"
VOICE_ID = "qyrL8YaluqDxJxVynLuN"  # Avantika


def get_video_duration(video_path):
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def split_video(video_path, output_dir, job_id):
    """Split video into multiple parts, each less than 30 seconds, output at 720p."""
    duration = get_video_duration(video_path)
    max_part_duration = 29.9

    # Calculate number of parts needed
    num_parts = int(duration // max_part_duration) + (1 if duration % max_part_duration > 0 else 0)

    # Ensure at least 2 parts for consistency
    if num_parts < 2:
        num_parts = 2
        max_part_duration = duration / 2

    parts = []
    current_time = 0

    for i in range(num_parts):
        part_num = i + 1
        part_path = output_dir / f"{job_id}_part{part_num}.mp4"

        # Calculate duration for this part
        remaining_duration = duration - current_time
        part_duration = min(max_part_duration, remaining_duration)

        # Skip if no duration left
        if part_duration <= 0:
            break

        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-ss", str(current_time),
            "-t", str(part_duration),
            "-vf", "scale=720:-2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(part_path)
        ]
        subprocess.run(cmd, capture_output=True)
        parts.append(part_path)
        current_time += part_duration

    return parts


def extract_audio(video_path, output_dir, job_id):
    """Extract audio from video as MP3."""
    audio_path = output_dir / f"{job_id}_audio.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "libmp3lame", "-q:a", "2",
        str(audio_path)
    ]
    subprocess.run(cmd, capture_output=True)
    return audio_path


def voice_change_elevenlabs(audio_path, output_dir, job_id):
    """Send audio to ElevenLabs voice changer (Speech-to-Speech)."""
    url = f"https://api.elevenlabs.io/v1/speech-to-speech/{VOICE_ID}"

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY
    }

    # Voice settings as per user config:
    # Stability: 46%, Similarity boost: 32%, Style: 15%, Speaker boost: enabled
    voice_settings = {
        "stability": 0.46,
        "similarity_boost": 0.32,
        "style": 0.15,
        "use_speaker_boost": True
    }

    with open(audio_path, "rb") as audio_file:
        files = {
            "audio": (audio_path.name, audio_file, "audio/mpeg")
        }
        data = {
            "model_id": "eleven_multilingual_sts_v2",
            "voice_settings": str(voice_settings).replace("'", '"').replace("True", "true")
        }

        response = requests.post(url, headers=headers, files=files, data=data)

    if response.status_code == 200:
        output_path = output_dir / f"{job_id}_voice_changed.mp3"
        with open(output_path, "wb") as f:
            f.write(response.content)
        return output_path, None
    else:
        return None, f"ElevenLabs error: {response.status_code} - {response.text}"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST", "OPTIONS"])
def process_video():
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video = request.files["video"]
    if video.filename == "":
        return jsonify({"error": "No video selected"}), 400

    # Get ElevenLabs toggle (default: off)
    use_elevenlabs = request.form.get("use_elevenlabs", "false").lower() == "true"

    # Generate unique job ID
    job_id = str(uuid.uuid4())[:8]

    # Save uploaded video
    video_ext = Path(video.filename).suffix or ".mp4"
    video_path = UPLOAD_FOLDER / f"{job_id}{video_ext}"
    video.save(video_path)

    try:
        # Get duration
        duration = get_video_duration(video_path)

        # Split video into parts (each <30 seconds)
        parts = split_video(video_path, OUTPUT_FOLDER, job_id)

        # Extract audio
        audio_path = extract_audio(video_path, OUTPUT_FOLDER, job_id)

        result = {
            "job_id": job_id,
            "duration": duration,
            "num_parts": len(parts),
            "parts": [f"/download/{job_id}_part{i+1}.mp4" for i in range(len(parts))],
            "original_audio": f"/download/{job_id}_audio.mp3",
            "elevenlabs_enabled": use_elevenlabs,
        }

        # Voice change with ElevenLabs only if enabled
        if use_elevenlabs:
            voice_path, error = voice_change_elevenlabs(audio_path, OUTPUT_FOLDER, job_id)
            if voice_path:
                result["voice_changed"] = f"/download/{job_id}_voice_changed.mp3"
            else:
                result["voice_error"] = error

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/download/<filename>")
def download(filename):
    file_path = OUTPUT_FOLDER / filename
    if file_path.exists():
        return send_file(file_path, as_attachment=True)
    return jsonify({"error": "File not found"}), 404


if __name__ == "__main__":
    app.run(debug=True, port=5000)
