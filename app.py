from flask import Flask, request, jsonify, send_file
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
    """Split video into two parts, each less than 30 seconds, output at 720p."""
    duration = get_video_duration(video_path)

    # Calculate split point (middle of video)
    split_point = duration / 2

    # Ensure each part is less than 30 seconds
    if split_point > 29.9:
        split_point = 29.9

    part1_path = output_dir / f"{job_id}_part1.mp4"
    part2_path = output_dir / f"{job_id}_part2.mp4"

    # Part 1: from start to split_point, scale to 720p (ultrafast for speed)
    cmd1 = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-t", str(split_point),
        "-vf", "scale=720:-2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k",
        str(part1_path)
    ]
    subprocess.run(cmd1, capture_output=True)

    # Part 2: from split_point to end (max 29.9 seconds), scale to 720p
    remaining = min(duration - split_point, 29.9)
    cmd2 = [
        "ffmpeg", "-y",
        "-ss", str(split_point),  # seek before input for speed
        "-i", str(video_path),
        "-t", str(remaining),
        "-vf", "scale=720:-2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k",
        str(part2_path)
    ]
    subprocess.run(cmd2, capture_output=True)

    return part1_path, part2_path


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
    return jsonify({"status": "ok", "message": "Splice API Backend"})


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

    # Generate unique job ID
    job_id = str(uuid.uuid4())[:8]

    # Save uploaded video
    video_ext = Path(video.filename).suffix or ".mp4"
    video_path = UPLOAD_FOLDER / f"{job_id}{video_ext}"
    video.save(video_path)

    try:
        # Get duration
        duration = get_video_duration(video_path)

        if duration > 60:
            return jsonify({"error": "Video must be 60 seconds or less"}), 400

        # Split video
        part1_path, part2_path = split_video(video_path, OUTPUT_FOLDER, job_id)

        # Extract audio
        audio_path = extract_audio(video_path, OUTPUT_FOLDER, job_id)

        # Voice change with ElevenLabs
        voice_path, error = voice_change_elevenlabs(audio_path, OUTPUT_FOLDER, job_id)

        result = {
            "job_id": job_id,
            "duration": duration,
            "part1": f"/download/{job_id}_part1.mp4",
            "part2": f"/download/{job_id}_part2.mp4",
            "original_audio": f"/download/{job_id}_audio.mp3",
        }

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
