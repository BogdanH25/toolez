import os
import uuid
import subprocess
import boto3
from flask import Flask, request, jsonify, render_template, send_file
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB max upload

# ---------------------------------------------------------------------------
# Cloudflare R2 config (S3-compatible)
# ---------------------------------------------------------------------------
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.getenv("R2_BUCKET_NAME", "video-tools")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "")  # optional: custom domain or public bucket URL

ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

# Local temp directory for processing
TEMP_DIR = os.path.join(os.path.dirname(__file__), "tmp")
os.makedirs(TEMP_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "webm", "mov", "avi", "mkv", "m4v", "flv", "wmv"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_video_duration(filepath):
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                filepath,
            ],
            capture_output=True, text=True,
        )
        import json
        info = json.loads(result.stdout)
        return float(info["format"]["duration"])
    except Exception:
        return 0


def cleanup(*files):
    """Remove temp files."""
    for f in files:
        try:
            if f and os.path.exists(f):
                os.remove(f)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    """Upload video to R2, return file key + duration."""
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    file = request.files["video"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Supported: MP4, WebM, MOV, AVI, MKV"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    file_id = uuid.uuid4().hex
    r2_key = f"uploads/{file_id}.{ext}"
    local_path = os.path.join(TEMP_DIR, f"{file_id}.{ext}")

    # Save locally first to get duration
    file.save(local_path)
    duration = get_video_duration(local_path)

    # Upload to R2
    s3.upload_file(local_path, R2_BUCKET, r2_key, ExtraArgs={"ContentType": file.content_type})
    cleanup(local_path)

    return jsonify({
        "success": True,
        "file_key": r2_key,
        "file_id": file_id,
        "filename": secure_filename(file.filename),
        "duration": round(duration, 2),
    })


@app.route("/api/trim", methods=["POST"])
def trim():
    """Trim video: download from R2, trim with FFmpeg, upload result to R2."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    file_key = data.get("file_key")
    start_time = data.get("start_time", 0)
    end_time = data.get("end_time")
    output_name = data.get("output_name", "trimmed-video")

    if not file_key or end_time is None:
        return jsonify({"error": "Missing file_key or end_time"}), 400

    try:
        start_time = float(start_time)
        end_time = float(end_time)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid time values"}), 400

    if start_time >= end_time:
        return jsonify({"error": "Start time must be less than end time"}), 400

    ext = file_key.rsplit(".", 1)[1].lower()
    file_id = uuid.uuid4().hex
    input_path = os.path.join(TEMP_DIR, f"input_{file_id}.{ext}")
    output_ext = "mp4"  # always output mp4 for max compatibility
    output_path = os.path.join(TEMP_DIR, f"output_{file_id}.{output_ext}")

    try:
        # Download from R2
        s3.download_file(R2_BUCKET, file_key, input_path)

        duration = end_time - start_time

        # Trim with FFmpeg — stream copy (instant, no re-encoding)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            # Fallback: re-encode if stream copy fails
            cmd_reencode = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", input_path,
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                output_path,
            ]
            result = subprocess.run(cmd_reencode, capture_output=True, text=True, timeout=600)

            if result.returncode != 0:
                return jsonify({"error": "FFmpeg processing failed", "details": result.stderr[-500:]}), 500

        # Upload trimmed video to R2
        safe_name = secure_filename(output_name) or "trimmed-video"
        r2_output_key = f"trimmed/{file_id}_{safe_name}.{output_ext}"

        s3.upload_file(
            output_path, R2_BUCKET, r2_output_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

        # Generate presigned download URL (valid 1 hour)
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": r2_output_key},
            ExpiresIn=3600,
        )

        # If public URL is configured, use that instead
        if R2_PUBLIC_URL:
            download_url = f"{R2_PUBLIC_URL.rstrip('/')}/{r2_output_key}"

        output_size = os.path.getsize(output_path)

        return jsonify({
            "success": True,
            "download_url": download_url,
            "output_key": r2_output_key,
            "output_name": f"{safe_name}.{output_ext}",
            "size": output_size,
            "duration": round(duration, 2),
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timed out. Try a shorter segment."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cleanup(input_path, output_path)


@app.route("/api/delete", methods=["POST"])
def delete_file():
    """Delete uploaded file from R2 (cleanup)."""
    data = request.get_json()
    file_key = data.get("file_key")
    if file_key:
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=file_key)
        except Exception:
            pass
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
