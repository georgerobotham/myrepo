"""
docx-map-updater — local web UI
--------------------------------
A small Flask server, bound to 127.0.0.1 only, serving a single-page
drag-and-drop interface for engine.py. Nothing leaves this machine: the
browser talks to a server running on your own PC, not the internet.

Run: python webapp.py  (opens your browser automatically)
"""

import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import engine

PORT = 8877
RUN_TTL_SECONDS = 30 * 60

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB

# run_id -> {"dir": Path, "docx_name": str, "report_name": str, "created": float}
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()


def _purge_old_runs():
    now = time.time()
    with RUNS_LOCK:
        expired = [rid for rid, r in RUNS.items() if now - r["created"] > RUN_TTL_SECONDS]
        for rid in expired:
            shutil.rmtree(RUNS[rid]["dir"], ignore_errors=True)
            del RUNS[rid]


def _save_upload(tempdir: Path, threshold_str: str):
    docx_file = request.files.get("docx")
    image_files = request.files.getlist("images")
    if not docx_file or not docx_file.filename:
        raise ValueError("No Word report was uploaded.")
    if not image_files:
        raise ValueError("No image files were uploaded.")
    try:
        threshold = float(threshold_str)
    except (TypeError, ValueError):
        raise ValueError("Match threshold must be a number between 0 and 1.")

    docx_name = secure_filename(docx_file.filename) or "report.docx"
    docx_path = tempdir / docx_name
    docx_file.save(docx_path)

    images_dir = tempdir / "images"
    images_dir.mkdir(exist_ok=True)
    saved_any = False
    for f in image_files:
        if not f.filename:
            continue
        name = secure_filename(f.filename)
        if not name or Path(name).suffix.lower() not in engine.IMAGE_EXTENSIONS:
            continue
        f.save(images_dir / name)
        saved_any = True
    if not saved_any:
        raise ValueError("None of the uploaded images had a supported extension "
                          "(jpg/jpeg/png/tif/bmp).")

    return docx_path, images_dir, threshold


def _parse_heading_index() -> int:
    raw = request.form.get("heading_index", "")
    if raw == "" or raw is None:
        raise ValueError("Please choose which section of the report to update.")
    try:
        return int(raw)
    except ValueError:
        raise ValueError("Invalid section selection.")


def _parse_caption_overrides() -> dict[int, str]:
    raw = request.form.get("caption_overrides", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {int(k): v for k, v in parsed.items()}
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid caption overrides.")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/headings", methods=["POST"])
def api_headings():
    docx_file = request.files.get("docx")
    if not docx_file or not docx_file.filename:
        return jsonify({"error": "No Word report was uploaded."}), 400
    tempdir = Path(tempfile.mkdtemp(prefix="docxmap_headings_"))
    try:
        docx_path = tempdir / (secure_filename(docx_file.filename) or "report.docx")
        docx_file.save(docx_path)
        document = engine.Document(str(docx_path))
        headings = engine.list_headings(document)
        return jsonify({"headings": headings})
    except Exception as exc:
        logging.exception("heading scan failed")
        return jsonify({"error": f"Could not read that Word document: {exc}"}), 400
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


@app.route("/api/preview", methods=["POST"])
def api_preview():
    _purge_old_runs()
    tempdir = Path(tempfile.mkdtemp(prefix="docxmap_preview_"))
    try:
        prefix = request.form.get("prefix", "Figure") or "Figure"
        general_style = request.form.get("general_style", "")
        heading_index = _parse_heading_index()
        docx_path, images_dir, threshold = _save_upload(
            tempdir, request.form.get("threshold", "0.55")
        )
        logs = []
        report = engine.run(
            docx_path=docx_path,
            images_dir=images_dir,
            heading_index=heading_index,
            figure_prefix=prefix,
            threshold=threshold,
            apply=False,
            general_style=general_style,
            report_path=tempdir / "report.json",
            log=logs.append,
        )
        report["logs"] = logs
        return jsonify(report)
    except (ValueError, engine.ValidationError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("preview failed")
        return jsonify({"error": f"Unexpected error: {exc}"}), 500
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


@app.route("/api/apply", methods=["POST"])
def api_apply():
    _purge_old_runs()
    tempdir = Path(tempfile.mkdtemp(prefix="docxmap_apply_"))
    try:
        prefix = request.form.get("prefix", "Figure") or "Figure"
        general_style = request.form.get("general_style", "")
        heading_index = _parse_heading_index()
        caption_overrides = _parse_caption_overrides()
        docx_path, images_dir, threshold = _save_upload(
            tempdir, request.form.get("threshold", "0.55")
        )
        logs = []
        report = engine.run(
            docx_path=docx_path,
            images_dir=images_dir,
            heading_index=heading_index,
            figure_prefix=prefix,
            threshold=threshold,
            apply=True,
            in_place=False,
            general_style=general_style,
            caption_overrides=caption_overrides,
            report_path=tempdir / "report.json",
            log=logs.append,
        )
        report["logs"] = logs

        run_id = uuid.uuid4().hex
        with RUNS_LOCK:
            RUNS[run_id] = {
                "dir": tempdir,
                "docx_name": Path(report["output_path"]).name,
                "report_name": "report.json",
                "created": time.time(),
            }
        report["run_id"] = run_id
        report["download_docx"] = f"/api/download/{run_id}/docx"
        report["download_report"] = f"/api/download/{run_id}/report"
        return jsonify(report)
    except (ValueError, engine.ValidationError) as exc:
        shutil.rmtree(tempdir, ignore_errors=True)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("apply failed")
        shutil.rmtree(tempdir, ignore_errors=True)
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@app.route("/api/download/<run_id>/<kind>")
def api_download(run_id, kind):
    _purge_old_runs()
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if run is None:
        return jsonify({"error": "This result has expired. Please run again."}), 404

    if kind == "docx":
        path = run["dir"] / run["docx_name"]
        download_name = run["docx_name"]
    elif kind == "report":
        path = run["dir"] / run["report_name"]
        download_name = run["report_name"]
    else:
        return jsonify({"error": "Unknown download kind"}), 400

    if not path.exists():
        return jsonify({"error": "File not found (it may have expired)."}), 404
    return send_file(path, as_attachment=True, download_name=download_name)


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"docx-map-updater running at http://127.0.0.1:{PORT} (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
