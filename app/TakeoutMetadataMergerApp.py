"""
Google Takeout → Apple Photos Metadata Merger (Two-Stage with Plan→Merge)
--------------------------------------------------------------------------

Objective:
    Desktop app (PySide6) that prepares Google Takeout exports for import into Apple Photos.
    Stage 1 builds a plan (media → JSON sidecar mapping) and shows a summary before any writes.
    Stage 2 merges JSON metadata into files via exiftool, then moves files into Completed/Failed.

Controls:
    • START   → Stage 1 (Plan). Shows a summary dialog; user confirms Stage 2 (Merge).
    • PAUSE   → Temporarily pauses the current stage; toggle again to Resume.
    • STOP    → Graceful stop (finish current item and exit).

Live UI:
    • Progress bar with true % for current stage (Plan or Merge).
    • Substage/heartbeat line (current folder, files/sec, ETA).
    • Counters (Completed, Failed, Warnings) + Remaining (Images, Videos).
    • Now Processing panel: thumbnail + filename + live activity stream.
    • Log window with rolling status.

Defaults:
    When Source is set, the app auto-fills:
        <Source>\\.. \\EasyTakeout-Results\\Completed
                      \\EasyTakeout-Results\\Failed
                      \\EasyTakeout-Results\\Logs
    You can overwrite these paths before pressing START.

Requirements:
    - Python 3.10+  ·  PySide6  ·  Pillow (for image thumbnails)
    - ExifTool (exiftool.exe on PATH or set explicit path)
    - ffmpeg.exe optional for video thumbnails

Retry:
    - To retry tough cases, set Source = Failed folder and press START again.
"""

import sys, os, json, time, csv, shutil, subprocess, traceback, hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

from PySide6.QtCore import Qt, QThread, Signal, QWaitCondition, QMutex, QPropertyAnimation, QEasingCurve, QTimer, Property
from PySide6.QtGui import QPixmap, QFont, QFontDatabase, QPainter, QPen, QColor, QLinearGradient, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFileDialog, QCheckBox, QProgressBar, QTextEdit, QGroupBox, QGridLayout, QMessageBox,
    QFrame, QDialog, QDialogButtonBox, QScrollArea, QSizePolicy, QGraphicsDropShadowEffect,
    QSpacerItem, QListWidget, QListWidgetItem
)

# ---------- Media sets ----------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".dng", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".m4v"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
ALBUM_JSON_NAMES = {"metadata.json", "album.json"}

# ---------- Helpers ----------
def is_media_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in MEDIA_EXTS
def is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS
def is_video_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in VIDEO_EXTS
def is_album_json(p: Path) -> bool:
    return p.name.lower() in ALBUM_JSON_NAMES
def ensure_dir(p: Path): p.mkdir(parents=True, exist_ok=True)
def hash_path(p: Path) -> str: return hashlib.sha1(str(p).encode("utf-8", "ignore")).hexdigest()[:16]

def find_sidecar_json(media_path: Path) -> Optional[Path]:
    exact = media_path.with_suffix(media_path.suffix + ".json")
    if exact.exists(): return exact
    base = media_path.stem
    cands = [c for c in media_path.parent.glob(f"{base}*.json") if not is_album_json(c)]
    if cands:
        cands.sort(key=lambda p: len(p.name))
        return cands[0]
    return None

def extract_google_fields(sidecar_json: Path) -> dict:
    with open(sidecar_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = {"taken_timestamp": None, "description": None, "latitude": None, "longitude": None, "altitude": None, "keywords": []}
    pt = data.get("photoTakenTime") or data.get("creationTime")
    if isinstance(pt, dict) and "timestamp" in pt:
        try: out["taken_timestamp"] = int(pt["timestamp"])
        except: pass
    out["description"] = data.get("description") or data.get("caption") or None
    for p in (data.get("people") or []):
        name = p.get("name")
        if name: out["keywords"].append(str(name))
    def pick_geo(g):
        if not isinstance(g, dict): return None
        lat, lon, alt = g.get("latitude"), g.get("longitude"), g.get("altitude")
        if isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and (lat!=0 or lon!=0):
            return lat, lon, (alt if isinstance(alt,(int,float)) else None)
        return None
    geo = pick_geo(data.get("geoDataExif")) or pick_geo(data.get("geoData"))
    if geo: out["latitude"], out["longitude"], out["altitude"] = geo
    return out

def build_exiftool_args(exiftool, overwrite, fields, target: Path) -> List[str]:
    args = [exiftool]
    if overwrite: args += ["-overwrite_original"]
    args += ["-P", "-m", "-n"]
    ts = fields.get("taken_timestamp")
    if ts:
        stamp = datetime.utcfromtimestamp(ts).strftime("%Y:%m:%d %H:%M:%S")
        args += [f"-DateTimeOriginal={stamp}", f"-CreateDate={stamp}", f"-ModifyDate={stamp}"]
    lat, lon, alt = fields.get("latitude"), fields.get("longitude"), fields.get("altitude")
    if lat is not None and lon is not None:
        args += [f"-GPSLatitude={lat}", f"-GPSLongitude={lon}"]
        if alt is not None: args += [f"-GPSAltitude={alt}"]
    desc = fields.get("description")
    if desc: args += [f"-XMP:Description={desc}", f"-IPTC:Caption-Abstract={desc}"]
    kws = fields.get("keywords") or []
    if kws and overwrite: args += ["-XMP:Subject="]
    for kw in kws: args += [f"-XMP:Subject+=-{kw}-"]
    args.append(str(target))
    return args

# ---------- Thumbnails ----------
def make_image_thumb(src: Path, cache_dir: Path, max_px=512) -> Optional[Path]:
    try:
        from PIL import Image, ImageOps
        out = cache_dir / f"{hash_path(src)}.png"
        if out.exists(): return out
        im = Image.open(src)
        try: im = ImageOps.exif_transpose(im)
        except: pass
        im.thumbnail((max_px, max_px))
        im.save(out, "PNG")
        return out
    except: return None

def make_video_thumb(src: Path, cache_dir: Path, ffmpeg_path: Optional[str], max_px=512) -> Optional[Path]:
    if not ffmpeg_path or shutil.which(ffmpeg_path) is None: return None
    out = cache_dir / f"{hash_path(src)}.png"
    if out.exists(): return out
    cmd = [ffmpeg_path, "-y", "-ss", "0.5", "-i", str(src), "-frames:v", "1", "-vf", f"scale='{max_px}:-2'", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return out if out.exists() else None
    except: return None

# ---------- Modern UI Components ----------
class ModernCard(QFrame):
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.NoFrame)
        self.setStyleSheet("""
            ModernCard {
                background-color: #2d2d2d;
                border-radius: 12px;
                border: 1px solid #3d3d3d;
            }
            ModernCard:hover {
                border: 1px solid #00d4aa;
                background-color: #323232;
            }
        """)
        
        # Add drop shadow effect
        self.shadow = QGraphicsDropShadowEffect()
        self.shadow.setBlurRadius(15)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(2)
        self.shadow.setColor(QColor(0, 0, 0, 100))
        self.setGraphicsEffect(self.shadow)
        
        # Main layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 15, 20, 15)
        self.layout.setSpacing(10)
        
        if title:
            self.title_label = QLabel(title)
            self.title_label.setStyleSheet("""
                QLabel {
                    font-size: 16px;
                    font-weight: 600;
                    color: #ffffff;
                    margin-bottom: 5px;
                }
            """)
            self.layout.addWidget(self.title_label)

class ModernButton(QPushButton):
    def __init__(self, text="", button_type="primary", parent=None):
        super().__init__(text, parent)
        self.button_type = button_type
        self.setMinimumHeight(42)
        self.setCursor(Qt.PointingHandCursor)
        
        # Add smooth transition animation
        self.animation = QPropertyAnimation(self, b"geometry")
        self.animation.setDuration(150)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        
        if button_type == "primary":
            self.setStyleSheet("""
                ModernButton {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #00d4aa, stop:1 #00b894);
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 12px 24px;
                }
                ModernButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #00e6c0, stop:1 #00d4aa);
                    transform: translateY(-1px);
                }
                ModernButton:pressed {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #00b894, stop:1 #00a085);
                }
                ModernButton:disabled {
                    background: #555555;
                    color: #999999;
                }
            """)
        elif button_type == "secondary":
            self.setStyleSheet("""
                ModernButton {
                    background: transparent;
                    color: #ffffff;
                    border: 2px solid #00d4aa;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 500;
                    padding: 10px 20px;
                }
                ModernButton:hover {
                    background: rgba(0, 212, 170, 0.1);
                    border-color: #00e6c0;
                }
                ModernButton:pressed {
                    background: rgba(0, 212, 170, 0.2);
                }
            """)
        elif button_type == "danger":
            self.setStyleSheet("""
                ModernButton {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #ff6b6b, stop:1 #e55656);
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 12px 24px;
                }
                ModernButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #ff7d7d, stop:1 #ff6b6b);
                }
            """)

class ModernDropZone(QFrame):
    pathSelected = Signal(str)
    
    def __init__(self, placeholder_text="Drag folder here or click to browse", parent=None):
        super().__init__(parent)
        self.placeholder_text = placeholder_text
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.current_path = ""
        
        self.setStyleSheet("""
            ModernDropZone {
                background-color: #252525;
                border: 2px dashed #555555;
                border-radius: 12px;
                color: #aaaaaa;
            }
            ModernDropZone:hover {
                border-color: #00d4aa;
                background-color: rgba(0, 212, 170, 0.05);
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        
        self.icon_label = QLabel("📁")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setStyleSheet("font-size: 32px; margin-bottom: 8px;")
        
        self.text_label = QLabel(placeholder_text)
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setStyleSheet("font-size: 14px; color: #aaaaaa;")
        self.text_label.setWordWrap(True)
        
        self.path_label = QLabel("")
        self.path_label.setAlignment(Qt.AlignCenter)
        self.path_label.setStyleSheet("font-size: 12px; color: #00d4aa; margin-top: 5px;")
        self.path_label.hide()
        
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label)
        layout.addWidget(self.path_label)
        
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            path = QFileDialog.getExistingDirectory(self, "Select Folder")
            if path:
                self.set_path(path)
                
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("""
                ModernDropZone {
                    background-color: rgba(0, 212, 170, 0.1);
                    border: 2px solid #00d4aa;
                    border-radius: 12px;
                }
            """)
            
    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            ModernDropZone {
                background-color: #252525;
                border: 2px dashed #555555;
                border-radius: 12px;
            }
            ModernDropZone:hover {
                border-color: #00d4aa;
                background-color: rgba(0, 212, 170, 0.05);
            }
        """)
        
    def dropEvent(self, event: QDropEvent):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        if files and os.path.isdir(files[0]):
            self.set_path(files[0])
        event.acceptProposedAction()
        
    def set_path(self, path):
        self.current_path = path
        self.icon_label.setText("✅")
        self.text_label.setText("Folder selected")
        self.path_label.setText(f"📁 {os.path.basename(path)}")
        self.path_label.show()
        self.pathSelected.emit(path)
        
class CircularProgress(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 120)
        self.progress = 0
        self.text = "0%"
        
        # Animation for smooth progress updates
        self.progress_animation = QPropertyAnimation(self, b"progress_value")
        self.progress_animation.setDuration(300)
        self.progress_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._progress_value = 0
        
    # Property for animation
    def get_progress_value(self):
        return self._progress_value
        
    def set_progress_value(self, value):
        self._progress_value = value
        self.update()
        
    progress_value = Property(float, get_progress_value, set_progress_value)
        
    def set_progress(self, value, text=""):
        new_progress = max(0, min(100, value))
        self.text = text or f"{int(new_progress)}%"
        
        # Animate to new progress value
        self.progress_animation.stop()
        self.progress_animation.setStartValue(self._progress_value)
        self.progress_animation.setEndValue(new_progress)
        self.progress_animation.start()
        
        self.progress = new_progress
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Background circle
        painter.setPen(QPen(QColor("#333333"), 8))
        painter.drawEllipse(10, 10, 100, 100)
        
        # Progress arc with gradient effect
        painter.setPen(QPen(QColor("#00d4aa"), 8))
        span_angle = int(self._progress_value * 360 / 100)
        painter.drawArc(10, 10, 100, 100, 90 * 16, -span_angle * 16)
        
        # Center text
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 14, QFont.Bold))
        painter.drawText(self.rect(), Qt.AlignCenter, self.text)

class ModernToggle(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setStyleSheet("""
            QCheckBox {
                color: #ffffff;
                font-size: 14px;
                font-weight: 500;
                spacing: 10px;
            }
            QCheckBox::indicator {
                width: 50px;
                height: 26px;
                border-radius: 13px;
                background-color: #555555;
                border: 2px solid #666666;
            }
            QCheckBox::indicator:checked {
                background-color: #00d4aa;
                border-color: #00d4aa;
            }
            QCheckBox::indicator::handle {
                width: 20px;
                height: 20px;
                border-radius: 10px;
                background-color: white;
                margin: 3px;
            }
        """)

class ModernStatsCard(ModernCard):
    def __init__(self, title, value="0", icon="📊", parent=None):
        super().__init__(parent=parent)
        
        layout = QHBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Icon
        icon_label = QLabel(icon)
        icon_label.setStyleSheet("font-size: 24px;")
        icon_label.setFixedSize(40, 40)
        icon_label.setAlignment(Qt.AlignCenter)
        
        # Content
        content_layout = QVBoxLayout()
        content_layout.setSpacing(2)
        
        self.value_label = QLabel(value)
        self.value_label.setStyleSheet("""
            font-size: 24px;
            font-weight: 700;
            color: #00d4aa;
            margin: 0;
        """)
        
        title_label = QLabel(title)
        title_label.setStyleSheet("""
            font-size: 12px;
            color: #aaaaaa;
            margin: 0;
        """)
        
        content_layout.addWidget(self.value_label)
        content_layout.addWidget(title_label)
        
        layout.addWidget(icon_label)
        layout.addLayout(content_layout)
        layout.addStretch()
        
        self.setLayout(layout)
        self.setFixedHeight(80)
        
    def update_value(self, value):
        self.value_label.setText(str(value))

# ---------- Plan summary dialog ----------
class PlanSummaryDialog(QDialog):
    def __init__(self, parent, mapped:int, missing:int, images:int, videos:int, live_pairs:int, size_gb:float):
        super().__init__(parent)
        self.setWindowTitle("Plan Complete")
        v = QVBoxLayout(self)
        info = QLabel(
            f"<b>Stage 1 (Plan) complete.</b><br>"
            f"With JSON (ready to merge): <b>{mapped}</b><br>"
            f"Missing JSON (will be failed in Stage 2): <b>{missing}</b><br>"
            f"Images: {images} · Videos: {videos} · Live Photo pairs: {live_pairs}<br>"
            f"Total size scanned: {size_gb:.1f} GB<br><br>"
            f"Proceed to <b>Stage 2 (Merge)</b>?"
        )
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

# ---------- Orchestrator (Plan → Merge) ----------
class Orchestrator(QThread):
    # UI signals
    stage = Signal(str)                       # "Planning…" / "Merging…"
    status = Signal(str)                      # log lines
    substage = Signal(str)                    # heartbeat: folder + rate + ETA
    progress = Signal(int, int)               # processed, total (stage-local)
    counts = Signal(int, int, int)            # completed, failed, warn (merge stage)
    remaining = Signal(int, int)              # images_left, videos_left
    thumb = Signal(str, str)                  # path, caption
    finished_files = Signal(str, str)         # report_csv, log_path
    need_user_confirm = Signal(dict)          # emitted after planning
    fatal = Signal(str)

    def __init__(self, source, completed, failed, logs, preserve_tree, overwrite, dry_run, exiftool, ffmpeg):
        super().__init__()
        self.source = Path(source); self.completed = Path(completed); self.failed = Path(failed)
        self.logs = Path(logs); self.preserve = preserve_tree; self.overwrite = overwrite
        self.dry_run = dry_run; self.exiftool = exiftool or ("exiftool.exe" if os.name=="nt" else "exiftool")
        self.ffmpeg = ffmpeg.strip() if ffmpeg else ""
        # flow control
        self._stop = False
        self._paused = False
        self._pause_cv = QWaitCondition()
        self._pause_mx = QMutex()
        # plan data
        self.plan: List[Tuple[Path, Optional[Path]]] = []
        self.analysis = {}
        # logging
        self.log_file = None
        self.log_file_path = None

    # public controls
    def request_stop(self): self._stop = True
    def toggle_pause(self, desired: Optional[bool]=None):
        new_state = (not self._paused) if desired is None else desired
        self._pause_mx.lock()
        self._paused = new_state
        if not self._paused:
            self._pause_cv.wakeAll()
        self._pause_mx.unlock()

    # internal pause gate
    def _maybe_pause(self):
        self._pause_mx.lock()
        try:
            while self._paused and not self._stop:
                self._pause_cv.wait(self._pause_mx, 200)
        finally:
            self._pause_mx.unlock()

    def _init_log_file(self):
        """Initialize log file for the entire session (planning + merge)"""
        ensure_dir(self.logs)
        self.log_file_path = self.logs / f"session_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            self.log_file = open(self.log_file_path, "w", encoding="utf-8")
            return True
        except Exception as e:
            self.status.emit(f"[ERROR] Could not create log file: {e}")
            return False

    def _close_log_file(self):
        """Close the log file"""
        if self.log_file:
            try:
                self.log_file.close()
            except:
                pass
            self.log_file = None

    def log(self, msg): 
        """Log message to both UI and file"""
        timestamp = datetime.now().isoformat(timespec='seconds')
        formatted_msg = f"[{timestamp}] {msg}"
        
        # Emit to UI
        self.status.emit(formatted_msg)
        
        # Write to log file
        if self.log_file:
            try:
                self.log_file.write(formatted_msg + "\n")
                self.log_file.flush()  # Ensure immediate write
            except Exception as e:
                # Fallback: just emit to UI if file write fails
                self.status.emit(f"[ERROR] Log write failed: {e}")

    # -------- Stage 1: Plan (three-pass with explicit subfolder inventory) --------
    def stage_plan(self):
        self.stage.emit("Planning…")
        src = self.source
        if not src.exists():
            raise RuntimeError("Source folder does not exist.")

        # Pass 0: Inventory all subdirectories first
        self.log("=== STARTING COMPREHENSIVE SUBFOLDER INVENTORY ===")
        self.log(f"Source directory: {src}")
        self.log("This will recursively discover ALL subdirectories before processing...")
        self.substage.emit("Inventorying all subdirectories...")
        
        all_directories = []
        total_dirs = 0
        last_ping = time.time()
        
        try:
            for root, dirs, files in os.walk(src):
                if self._stop: break
                self._maybe_pause()
                
                root_path = Path(root)
                all_directories.append(root_path)
                total_dirs += 1
                
                now = time.time()
                if now - last_ping > 0.5:  # Update every 500ms for responsiveness
                    rel_path = root_path.relative_to(src) if root_path != src else "."
                    self.substage.emit(f"Found {total_dirs} directories | Current: {rel_path}")
                    last_ping = now
                    
        except PermissionError as e:
            self.log(f"WARNING: Permission denied accessing some directories: {e}")
        except Exception as e:
            self.log(f"WARNING: Error during directory inventory: {e}")
            
        self.log(f"Directory inventory complete: Found {total_dirs} directories to process")
        self.substage.emit(f"Directory inventory complete: {total_dirs} directories found")

        # Pass 1: count media quickly (accurate denominator)
        def is_media_name(name: str) -> bool:
            n = name.lower()
            for ext in MEDIA_EXTS:
                if n.endswith(ext): return True
            return False

        total_media = 0
        dirs_processed = 0
        last_ping = time.time()
        
        self.log("Starting media file counting...")
        for directory in all_directories:
            if self._stop: break
            self._maybe_pause()
            
            dirs_processed += 1
            
            try:
                # Process files in this specific directory
                if directory.exists() and directory.is_dir():
                    for file_path in directory.iterdir():
                        if file_path.is_file() and is_media_name(file_path.name):
                            total_media += 1
                            
            except PermissionError:
                self.log(f"WARNING: Permission denied accessing: {directory}")
                continue
            except Exception as e:
                self.log(f"WARNING: Error processing directory {directory}: {e}")
                continue
                
            now = time.time()
            if now - last_ping > 1.0:
                rel_path = directory.relative_to(src) if directory != src else "."
                progress_pct = int((dirs_processed / total_dirs) * 100) if total_dirs > 0 else 0
                self.substage.emit(f"Counting media files… {total_media} found ({progress_pct}%) | in {rel_path}")
                last_ping = now

        if total_media == 0:
            self.analysis = {"total": 0, "images": 0, "videos": 0,
                             "with_json": 0, "without_json": 0,
                             "live_pairs": 0, "total_bytes": 0}
            self.need_user_confirm.emit(self.analysis)
            return

        # Pass 2: map sidecars with steady updates, rate, ETA using explicit directory list
        self.log("Starting sidecar JSON mapping...")
        self.plan.clear()
        images = videos = with_json = without_json = 0
        size_bytes = 0
        stems = {}

        processed = 0
        dirs_processed = 0
        t0 = time.time()
        last_ui = t0

        for directory in all_directories:
            if self._stop: break
            self._maybe_pause()
            
            dirs_processed += 1
            
            try:
                if not directory.exists() or not directory.is_dir():
                    continue
                    
                # Process all media files in this directory
                for file_path in directory.iterdir():
                    if self._stop: break
                    self._maybe_pause()
                    
                    if not is_media_file(file_path):
                        continue

                    processed += 1
                    try:
                        size_bytes += file_path.stat().st_size
                    except Exception:
                        pass

                    if is_image_file(file_path): images += 1
                    if is_video_file(file_path): videos += 1
                    
                    sc = find_sidecar_json(file_path)
                    if sc: 
                        with_json += 1
                        self.plan.append((file_path, sc))
                    else:  
                        without_json += 1
                        self.plan.append((file_path, None))
                    
                    tp = "img" if is_image_file(file_path) else ("vid" if is_video_file(file_path) else "x")
                    if tp in ("img","vid"): 
                        stems.setdefault(file_path.stem,set()).add(tp)

                    now = time.time()
                    if (processed % 100 == 0) or (now - last_ui > 1.0):  # More frequent updates
                        rate = processed / max(now - t0, 1e-6)
                        remain = total_media - processed
                        eta_sec = int(remain / max(rate, 1e-6))
                        eta_m, eta_s = eta_sec // 60, eta_sec % 60
                        
                        rel_path = directory.relative_to(src) if directory != src else "."
                        dir_progress = int((dirs_processed / total_dirs) * 100) if total_dirs > 0 else 0
                        
                        self.progress.emit(processed, total_media)
                        self.substage.emit(
                            f"Mapping JSON… {processed}/{total_media} ({dir_progress}% dirs)  |  "
                            f"{rate:.1f} files/s  |  ETA {eta_m}m {eta_s}s  |  in {rel_path}"
                        )
                        last_ui = now
                        
            except PermissionError:
                self.log(f"WARNING: Permission denied processing: {directory}")
                continue
            except Exception as e:
                self.log(f"WARNING: Error processing directory {directory}: {e}")
                continue

        live_pairs = sum(1 for types in stems.values() if "img" in types and "vid" in types)
        
        # Log comprehensive summary
        self.log(f"Planning phase complete!")
        self.log(f"Directories processed: {dirs_processed}/{total_dirs}")
        self.log(f"Media files found: {total_media}")
        self.log(f"  - Images: {images}")
        self.log(f"  - Videos: {videos}")
        self.log(f"  - With JSON metadata: {with_json}")
        self.log(f"  - Missing JSON metadata: {without_json}")
        self.log(f"  - Live Photo pairs: {live_pairs}")
        self.log(f"Total data size: {size_bytes/(1024**3):.2f} GB")
        
        self.analysis = {
            "total": total_media, "images": images, "videos": videos,
            "with_json": with_json, "without_json": without_json,
            "live_pairs": live_pairs, "total_bytes": size_bytes
        }
        self.progress.emit(total_media, total_media)
        self.substage.emit(f"Mapping complete: {dirs_processed} directories processed")
        self.need_user_confirm.emit(self.analysis)

    # -------- Stage 2: Merge + Move --------
    def move_pair(self, media: Path, sidecar: Optional[Path], ok: bool):
        dest_root = self.completed if ok else self.failed
        if self.preserve:
            rel = media.resolve().relative_to(self.source.resolve())
            dest_media = dest_root / rel
            dest_json  = dest_root / rel.parent / (media.name + ".json") if sidecar else None
        else:
            dest_media = dest_root / media.name
            dest_json  = dest_root / (media.name + ".json") if sidecar else None
        ensure_dir(dest_media.parent)
        if not self.dry_run:
            shutil.move(str(media), str(dest_media))
            if sidecar and sidecar.exists():
                ensure_dir(dest_json.parent)
                shutil.move(str(sidecar), str(dest_json))

    def run_exiftool(self, args):
        if self.dry_run: return 0, "DRY_RUN", ""
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def live_partner_of(self, media: Path) -> Optional[Path]:
        for ext in (".mov", ".mp4", ".jpg", ".jpeg"):
            cand = media.with_suffix(ext)
            if cand.exists() and cand != media and cand.stem == media.stem:
                return cand
        return None

    def stage_merge(self):
        self.stage.emit("Merging…")
        self.substage.emit("")
        for p in [self.source, self.completed, self.failed, self.logs]:
            ensure_dir(p)
        if not self.dry_run and shutil.which(self.exiftool) is None:
            raise RuntimeError("ExifTool not found on PATH. Set a valid exiftool path.")
        report_csv = self.logs / f"merge_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        log_path   = self.logs / f"merge_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        thumb_cache = self.logs / "thumbcache"; ensure_dir(thumb_cache)

        ok_ct = fail_ct = warn_ct = 0
        total = len(self.plan)
        imgs_left = self.analysis.get("images", 0)
        vids_left = self.analysis.get("videos", 0)
        t0 = time.time()

        with open(report_csv, "w", newline="", encoding="utf-8") as fcsv, open(log_path, "w", encoding="utf-8") as flog:
            writer = csv.writer(fcsv); writer.writerow(["media_path","json_sidecar","status","note"])
            for idx, (media, sidecar) in enumerate(self.plan, start=1):
                if self._stop: break
                self._maybe_pause()

                # thumbnail + remaining
                thumb_path = ""
                if is_image_file(media):
                    t = make_image_thumb(media, thumb_cache)
                    if imgs_left: imgs_left -= 1
                    if t: thumb_path = str(t)
                elif is_video_file(media):
                    t = make_video_thumb(media, thumb_cache, self.ffmpeg or None)
                    if vids_left: vids_left -= 1
                    if t: thumb_path = str(t)
                self.thumb.emit(thumb_path, media.name)
                self.remaining.emit(imgs_left, vids_left)

                # progress subline (ETA)
                rate = idx / max(time.time() - t0, 1e-6)
                remain = total - idx
                eta = int(remain / max(rate, 1e-6))
                self.substage.emit(f"Merging… {idx}/{total}  |  {rate:.1f} files/s  |  ETA {eta//60}m {eta%60}s")

                if sidecar is None or not sidecar.exists():
                    msg = "No matching JSON sidecar"
                    self.log(f"FAIL (no JSON): {media}")
                    writer.writerow([str(media), "", "FAILED", msg])
                    self.move_pair(media, None, ok=False)
                    fail_ct += 1
                    self.counts.emit(ok_ct, fail_ct, warn_ct)
                    self.progress.emit(idx, total)
                    continue

                try:
                    fields = extract_google_fields(sidecar)
                except Exception as e:
                    msg = f"JSON parse error: {e}"
                    self.log(f"FAIL (bad JSON): {media} -> {e}")
                    writer.writerow([str(media), str(sidecar), "FAILED", msg])
                    self.move_pair(media, sidecar, ok=False)
                    fail_ct += 1
                    self.counts.emit(ok_ct, fail_ct, warn_ct)
                    self.progress.emit(idx, total)
                    continue

                args = build_exiftool_args(self.exiftool, self.overwrite, fields, media)
                rc, out, err = self.run_exiftool(args)
                if rc != 0:
                    msg = f"exiftool error: {err.strip() or out.strip()}"
                    self.log(f"FAIL (exiftool): {media} -> {msg}")
                    writer.writerow([str(media), str(sidecar), "FAILED", msg])
                    self.move_pair(media, sidecar, ok=False)
                    fail_ct += 1
                    self.counts.emit(ok_ct, fail_ct, warn_ct)
                    self.progress.emit(idx, total)
                    continue

                partner_msg = ""
                partner = self.live_partner_of(media)
                if partner:
                    args2 = build_exiftool_args(self.exiftool, self.overwrite, fields, partner)
                    rc2, out2, err2 = self.run_exiftool(args2)
                    if rc2 != 0:
                        partner_msg = f"Live partner failed: {partner.name}: {err2.strip() or out2.strip()}"
                        self.log(f"WARN: {partner_msg}")
                        warn_ct += 1

                self.log(f"OK: {media}")
                writer.writerow([str(media), str(sidecar),
                                 "COMPLETED" if not partner_msg else "COMPLETED_WITH_PARTNER_WARN",
                                 partner_msg])
                self.move_pair(media, sidecar, ok=True)
                ok_ct += 1
                self.counts.emit(ok_ct, fail_ct, warn_ct)
                self.progress.emit(idx, total)

        self.finished_files.emit(str(report_csv), str(log_path))

    # -------- Thread entry --------
    def run(self):
        try:
            # Stage 1: Plan
            self.stage_plan()
            # Wait here for UI confirmation (pause-unpause trick)
            self._pause_mx.lock()
            try:
                while self._paused and not self._stop:
                    self._pause_cv.wait(self._pause_mx, 200)
            finally:
                self._pause_mx.unlock()
            if self._stop: return
            # Stage 2: Merge
            self.stage_merge()
        except Exception as e:
            self.fatal.emit(f"Fatal: {e}\n{traceback.format_exc()}")

# ---------- GUI ----------
class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🚀 EasyTakeout - Modern Metadata Merger")
        self.resize(1400, 900)
        self.setMinimumSize(1200, 800)
        self.worker: Optional[Orchestrator] = None
        self._setup_modern_theme()
        self._build_ui()
        
    def _setup_modern_theme(self):
        """Apply the modern dark theme to the entire application"""
        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a1a;
                color: #ffffff;
                font-family: 'Segoe UI', 'Inter', 'Arial', sans-serif;
                font-size: 13px;
            }
            
            QMainWindow {
                background-color: #1a1a1a;
            }
            
            QLineEdit {
                background-color: #2d2d2d;
                border: 2px solid #3d3d3d;
                border-radius: 8px;
                padding: 12px 16px;
                font-size: 14px;
                color: #ffffff;
                selection-background-color: #00d4aa;
            }
            
            QLineEdit:focus {
                border-color: #00d4aa;
                background-color: #323232;
            }
            
            QTextEdit {
                background-color: #242424;
                border: 1px solid #3d3d3d;
                border-radius: 8px;
                padding: 12px;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 12px;
                line-height: 1.4;
            }
            
            QProgressBar {
                background-color: #2d2d2d;
                border: none;
                border-radius: 8px;
                height: 8px;
                text-align: center;
            }
            
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d4aa, stop:1 #00b894);
                border-radius: 8px;
            }
            
            QScrollBar:vertical {
                background-color: #2d2d2d;
                width: 12px;
                border-radius: 6px;
            }
            
            QScrollBar::handle:vertical {
                background-color: #555555;
                border-radius: 6px;
                min-height: 20px;
            }
            
            QScrollBar::handle:vertical:hover {
                background-color: #666666;
            }
            
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

    def _on_source_selected(self, path: str):
        """Handle source folder selection from dropzone"""
        self.inp_source.setText(path)  # Update hidden field for compatibility
        self._autofill_results_from_source(path)
        
        # Add visual feedback
        self.append_log(f"📁 Source folder selected: {os.path.basename(path)}")
        
        # Animate the dropzone to show success
        self.source_dropzone.setStyleSheet("""
            ModernDropZone {
                background-color: rgba(0, 212, 170, 0.15);
                border: 2px solid #00d4aa;
                border-radius: 12px;
            }
        """)
        
        # Reset after a short delay
        QTimer.singleShot(2000, lambda: self.source_dropzone.setStyleSheet("""
            ModernDropZone {
                background-color: #252525;
                border: 2px dashed #555555;
                border-radius: 12px;
            }
            ModernDropZone:hover {
                border-color: #00d4aa;
                background-color: rgba(0, 212, 170, 0.05);
            }
        """))
        
    def _browse(self, target: QLineEdit):
        p = QFileDialog.getExistingDirectory(self, "Select Folder")
        if p:
            target.setText(p)
            if target is self.inp_source:
                self._autofill_results_from_source(p)

    def _autofill_results_from_source(self, src_path: str):
        # Only set defaults if fields are empty or point to previous auto set
        src = Path(src_path).resolve()
        parent = src.parent
        results = parent / "EasyTakeout-Results"
        comp = results / "Completed"
        fail = results / "Failed"
        logs = results / "Logs"
        # Only overwrite if empty or previously auto-filled into a different source
        def should_set(box: QLineEdit):
            val = Path(box.text()) if box.text().strip() else None
            return (val is None) or ("EasyTakeout-Results" in (str(val) if val else ""))
        if should_set(self.inp_completed): self.inp_completed.setText(str(comp))
        if should_set(self.inp_failed):    self.inp_failed.setText(str(fail))
        if should_set(self.inp_logs):      self.inp_logs.setText(str(logs))

    def _build_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)
        
        # Left Panel - Configuration
        left_panel = QVBoxLayout()
        left_panel.setSpacing(20)
        
        # Header
        header_card = ModernCard()
        header_layout = QVBoxLayout()
        
        title = QLabel("🚀 EasyTakeout")
        title.setStyleSheet("""
            font-size: 28px;
            font-weight: 700;
            color: #00d4aa;
            margin-bottom: 5px;
        """)
        
        subtitle = QLabel("Transform your Google Takeout into Apple Photos ready files")
        subtitle.setStyleSheet("""
            font-size: 14px;
            color: #aaaaaa;
            margin-bottom: 15px;
        """)
        subtitle.setWordWrap(True)
        
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        header_card.layout.addLayout(header_layout)
        left_panel.addWidget(header_card)
        
        # Source folder selection
        source_card = ModernCard("📁 Source Folder")
        self.source_dropzone = ModernDropZone(
            "Drag your Google Takeout folder here\nor click to browse"
        )
        self.source_dropzone.pathSelected.connect(self._on_source_selected)
        source_card.layout.addWidget(self.source_dropzone)
        left_panel.addWidget(source_card)
        
        # Destination folders card
        dest_card = ModernCard("🎯 Output Folders")
        dest_grid = QGridLayout()
        dest_grid.setSpacing(12)
        
        # Create line edits for destinations
        self.inp_source = QLineEdit()  # Hidden, for compatibility
        self.inp_source.hide()
        
        self.inp_completed = QLineEdit()
        self.inp_completed.setPlaceholderText("Completed files destination...")
        
        self.inp_failed = QLineEdit()
        self.inp_failed.setPlaceholderText("Failed files destination...")
        
        self.inp_logs = QLineEdit()
        self.inp_logs.setPlaceholderText("Log files destination...")
        
        # Add labels and inputs
        completed_label = QLabel("✅ Completed:")
        completed_label.setStyleSheet("color: #ffffff; font-weight: 600;")
        failed_label = QLabel("❌ Failed:")
        failed_label.setStyleSheet("color: #ffffff; font-weight: 600;")
        logs_label = QLabel("📝 Logs:")
        logs_label.setStyleSheet("color: #ffffff; font-weight: 600;")
        
        dest_grid.addWidget(completed_label, 0, 0)
        dest_grid.addWidget(self.inp_completed, 0, 1)
        dest_grid.addWidget(failed_label, 1, 0)
        dest_grid.addWidget(self.inp_failed, 1, 1)
        dest_grid.addWidget(logs_label, 2, 0)
        dest_grid.addWidget(self.inp_logs, 2, 1)
        
        dest_card.layout.addLayout(dest_grid)
        left_panel.addWidget(dest_card)

        # Options card
        options_card = ModernCard("⚙️ Processing Options")
        options_layout = QVBoxLayout()
        
        # Toggles
        self.chk_preserve = ModernToggle("Preserve folder structure")
        self.chk_preserve.setChecked(True)
        
        self.chk_overwrite = ModernToggle("Overwrite existing metadata")
        self.chk_overwrite.setChecked(True)
        
        self.chk_dryrun = ModernToggle("Dry run (preview only)")
        
        options_layout.addWidget(self.chk_preserve)
        options_layout.addWidget(self.chk_overwrite)
        options_layout.addWidget(self.chk_dryrun)
        
        # Tool paths
        tools_layout = QVBoxLayout()
        tools_layout.setSpacing(8)
        
        exiftool_label = QLabel("🔧 ExifTool Path (optional):")
        exiftool_label.setStyleSheet("color: #ffffff; font-weight: 600; margin-top: 15px;")
        self.inp_exiftool = QLineEdit()
        self.inp_exiftool.setPlaceholderText("Leave blank to use system PATH")
        
        ffmpeg_label = QLabel("🎬 FFmpeg Path (optional):")
        ffmpeg_label.setStyleSheet("color: #ffffff; font-weight: 600;")
        self.inp_ffmpeg = QLineEdit()
        self.inp_ffmpeg.setPlaceholderText("For video thumbnails (optional)")
        
        tools_layout.addWidget(exiftool_label)
        tools_layout.addWidget(self.inp_exiftool)
        tools_layout.addWidget(ffmpeg_label)
        tools_layout.addWidget(self.inp_ffmpeg)
        
        options_layout.addLayout(tools_layout)
        options_card.layout.addLayout(options_layout)
        left_panel.addWidget(options_card)

        # Control buttons
        controls_card = ModernCard("🎮 Controls")
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(15)
        
        self.btn_start = ModernButton("🚀 Start Processing", "primary")
        self.btn_pause = ModernButton("⏸️ Pause", "secondary")
        self.btn_stop = ModernButton("⏹️ Stop", "danger")
        
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(self.on_pause_toggle)
        self.btn_stop.clicked.connect(self.on_stop)
        
        controls_layout.addWidget(self.btn_start)
        controls_layout.addWidget(self.btn_pause)
        controls_layout.addWidget(self.btn_stop)
        
        controls_card.layout.addLayout(controls_layout)
        left_panel.addWidget(controls_card)
        
        # Add left panel to main layout
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        left_widget.setMaximumWidth(500)
        main_layout.addWidget(left_widget)

        # Right Panel - Progress & Activity
        right_panel = QVBoxLayout()
        right_panel.setSpacing(20)
        
        # Progress card
        progress_card = ModernCard("📊 Progress Dashboard")
        progress_layout = QVBoxLayout()
        
        # Stage indicator
        self.lbl_stage = QLabel("🔄 Ready to Start")
        self.lbl_stage.setStyleSheet("""
            font-size: 20px;
            font-weight: 600;
            color: #00d4aa;
            margin-bottom: 10px;
        """)
        self.lbl_stage.setAlignment(Qt.AlignCenter)
        
        # Circular progress
        progress_container = QHBoxLayout()
        self.circular_progress = CircularProgress()
        progress_container.addStretch()
        progress_container.addWidget(self.circular_progress)
        progress_container.addStretch()
        
        # Traditional progress bar (for backup)
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setTextVisible(False)
        self.prog.setMaximumHeight(8)
        
        # Substage info
        self.lbl_substage = QLabel("Waiting for input...")
        self.lbl_substage.setStyleSheet("""
            color: #aaaaaa;
            font-size: 12px;
            font-style: italic;
        """)
        self.lbl_substage.setAlignment(Qt.AlignCenter)
        self.lbl_substage.setWordWrap(True)
        
        progress_layout.addWidget(self.lbl_stage)
        progress_layout.addLayout(progress_container)
        progress_layout.addWidget(self.prog)
        progress_layout.addWidget(self.lbl_substage)
        
        progress_card.layout.addLayout(progress_layout)
        right_panel.addWidget(progress_card)
        
        # Stats cards row
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(15)
        
        self.stats_completed = ModernStatsCard("Completed", "0", "✅")
        self.stats_failed = ModernStatsCard("Failed", "0", "❌")
        self.stats_warnings = ModernStatsCard("Warnings", "0", "⚠️")
        
        stats_layout.addWidget(self.stats_completed)
        stats_layout.addWidget(self.stats_failed)
        stats_layout.addWidget(self.stats_warnings)
        
        right_panel.addLayout(stats_layout)
        
        # Remaining files row
        remaining_layout = QHBoxLayout()
        remaining_layout.setSpacing(15)
        
        self.stats_images = ModernStatsCard("Images Left", "0", "🖼️")
        self.stats_videos = ModernStatsCard("Videos Left", "0", "🎬")
        
        remaining_layout.addWidget(self.stats_images)
        remaining_layout.addWidget(self.stats_videos)
        remaining_layout.addStretch()
        
        right_panel.addLayout(remaining_layout)

        # Now Processing card
        processing_card = ModernCard("⚡ Live Processing")
        processing_layout = QVBoxLayout()
        
        # Thumbnail area
        thumb_container = QFrame()
        thumb_container.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2a2a2a, stop:1 #1f1f1f);
                border-radius: 12px;
                border: 2px solid #333333;
            }
        """)
        thumb_container.setMinimumHeight(200)
        thumb_container.setMaximumHeight(250)
        
        thumb_layout = QVBoxLayout(thumb_container)
        thumb_layout.setAlignment(Qt.AlignCenter)
        
        self.thumb = QLabel("🎯")
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("""
            font-size: 48px;
            color: #555555;
            background: transparent;
            border: none;
        """)
        
        self.thumb_caption = QLabel("Ready to process files")
        self.thumb_caption.setAlignment(Qt.AlignCenter)
        self.thumb_caption.setStyleSheet("""
            font-size: 14px;
            color: #aaaaaa;
            margin-top: 10px;
            background: transparent;
            border: none;
        """)
        self.thumb_caption.setWordWrap(True)
        
        thumb_layout.addWidget(self.thumb)
        thumb_layout.addWidget(self.thumb_caption)
        
        # Activity stream
        activity_label = QLabel("📋 Activity Log")
        activity_label.setStyleSheet("""
            font-size: 14px;
            font-weight: 600;
            color: #ffffff;
            margin-top: 15px;
            margin-bottom: 5px;
        """)
        
        self.activity_stream = QTextEdit()
        self.activity_stream.setReadOnly(True)
        self.activity_stream.setMinimumHeight(120)
        self.activity_stream.setMaximumHeight(150)
        self.activity_stream.setPlaceholderText("Activity will appear here when processing starts...")
        
        processing_layout.addWidget(thumb_container)
        processing_layout.addWidget(activity_label)
        processing_layout.addWidget(self.activity_stream)
        
        processing_card.layout.addLayout(processing_layout)
        right_panel.addWidget(processing_card)

        # Full log window
        log_card = ModernCard("📜 Detailed Log")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Detailed processing log will appear here...")
        log_card.layout.addWidget(self.log)
        right_panel.addWidget(log_card, 1)  # This gets most of the space
        
        # Tip
        tip_card = ModernCard()
        tip = QLabel("💡 <b>Pro Tip:</b> You can set Source = Failed folder to retry only failed files later. Use drag & drop for fastest folder selection!")
        tip.setStyleSheet("""
            color: #aaaaaa;
            font-size: 12px;
            padding: 8px;
            background-color: rgba(0, 212, 170, 0.05);
            border-radius: 6px;
            border-left: 3px solid #00d4aa;
        """)
        tip.setWordWrap(True)
        tip_card.layout.addWidget(tip)
        right_panel.addWidget(tip_card)
        
        # Add right panel to main layout
        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        main_layout.addWidget(right_widget, 1)  # Takes remaining space
        
        # Setup tooltips for better UX
        self.source_dropzone.setToolTip("Drop a Google Takeout folder here or click to browse")
        self.inp_completed.setToolTip("Where successfully processed files will be saved")
        self.inp_failed.setToolTip("Where files that couldn't be processed will be saved")
        self.inp_logs.setToolTip("Where processing logs and reports will be saved")
        self.chk_preserve.setToolTip("Keep the original folder structure in output")
        self.chk_overwrite.setToolTip("Replace existing EXIF metadata in files")
        self.chk_dryrun.setToolTip("Preview changes without actually modifying files")

    def _maybe_autofill_from_text(self):
        t = self.inp_source.text().strip()
        if t: self._autofill_results_from_source(t)

    # ---- UI helpers ----
    def set_thumb(self, path: str, caption: str):
        self.thumb_caption.setText(caption or "Ready to process files")
        if path and Path(path).exists():
            pm = QPixmap(path)
            if not pm.isNull():
                # Scale to fit the container nicely
                scaled_pm = pm.scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb.setPixmap(scaled_pm)
                self.thumb.setStyleSheet("""
                    background: transparent;
                    border: none;
                    border-radius: 8px;
                """)
                return
        # Fallback to emoji if no image
        self.thumb.setText("🎞️")
        self.thumb.setStyleSheet("""
            font-size: 48px;
            color: #555555;
            background: transparent;
            border: none;
        """)

    def append_log(self, line: str):
        self.log.append(line)
        # also tee into the Now Processing activity stream (keep it lively)
        self.activity_stream.append(line)
        # Auto-scroll to bottom with smooth animation
        QTimer.singleShot(50, lambda: self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum()))
        QTimer.singleShot(50, lambda: self.activity_stream.verticalScrollBar().setValue(
            self.activity_stream.verticalScrollBar().maximum()))

    def set_counts(self, ok, fail, warn): 
        self.stats_completed.update_value(ok)
        self.stats_failed.update_value(fail)
        self.stats_warnings.update_value(warn)
        
    def set_remaining(self, img, vid): 
        self.stats_images.update_value(img)
        self.stats_videos.update_value(vid)
        
    def on_substage(self, text: str): 
        self.lbl_substage.setText(text)

    # ---- Controls ----
    def on_start(self):
        src = self.inp_source.text().strip()
        ok  = self.inp_completed.text().strip()
        fl  = self.inp_failed.text().strip()
        lg  = self.inp_logs.text().strip()
        if not all([src, ok, fl, lg]):
            QMessageBox.warning(self, "⚠️ Missing Information", 
                               "Please select source folder and set all destination paths.")
            return
        # Ensure default folders exist
        for p in [ok, fl, lg]:
            ensure_dir(Path(p))

        # Clear UI
        self.log.clear(); self.activity_stream.clear()
        self.prog.setValue(0); self.set_counts(0,0,0); self.set_remaining(0,0)
        self.circular_progress.set_progress(0, "Starting...")
        
        # Update stage
        self.lbl_stage.setText("🗒️ Planning Stage")
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setText("⏸️ Pause")

        self.worker = Orchestrator(
            src, ok, fl, lg,
            preserve_tree=self.chk_preserve.isChecked(),
            overwrite=self.chk_overwrite.isChecked(),
            dry_run=self.chk_dryrun.isChecked(),
            exiftool=self.inp_exiftool.text().strip(),
            ffmpeg=self.inp_ffmpeg.text().strip()
        )
        # Signals
        self.worker.stage.connect(self.lbl_stage.setText)
        self.worker.status.connect(self.append_log)
        self.worker.substage.connect(self.on_substage)
        self.worker.progress.connect(self.on_progress)
        self.worker.counts.connect(self.set_counts)
        self.worker.remaining.connect(self.set_remaining)
        self.worker.thumb.connect(self.on_thumb)
        self.worker.finished_files.connect(self.on_finished)
        self.worker.fatal.connect(self.on_fatal)
        self.worker.need_user_confirm.connect(self.on_plan_complete_show_dialog)
        # Pause at the plan→merge boundary; we'll resume after user confirms
        self.worker.toggle_pause(True)
        self.worker.start()

    def on_pause_toggle(self):
        if not self.worker: return
        if self.btn_pause.text().startswith("⏸️"):
            self.worker.toggle_pause(True)
            self.btn_pause.setText("▶️ Resume")
            self.lbl_stage.setText("⏸️ Paused")
            self.append_log("[UI] Processing paused")
        else:
            self.worker.toggle_pause(False)
            self.btn_pause.setText("⏸️ Pause")
            self.lbl_stage.setText("▶️ Resuming...")
            self.append_log("[UI] Processing resumed")

    def on_stop(self):
        if self.worker:
            self.worker.request_stop()
            self.append_log("[UI] Stop requested - finishing current operation...")
            self.lbl_stage.setText("⏹️ Stopping...")
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)

    # ---- Slots ----
    def on_progress(self, processed, total):
        pct = int((processed/total)*100) if total else 0
        self.prog.setValue(pct)
        self.circular_progress.set_progress(pct, f"{processed}/{total}")

    def on_thumb(self, path, caption): 
        self.set_thumb(path, caption)

    def on_plan_complete_show_dialog(self, summary: dict):
        size_gb = summary.get("total_bytes",0)/(1024**3)
        dlg = PlanSummaryDialog(self, summary.get("with_json",0), summary.get("without_json",0),
                                summary.get("images",0), summary.get("videos",0),
                                summary.get("live_pairs",0), size_gb)
        
        # Update progress to show planning complete
        self.circular_progress.set_progress(100, "Plan Complete")
        self.lbl_stage.setText("✅ Planning Complete")
        
        if dlg.exec() != QDialog.Accepted:
            self.append_log("[UI] User cancelled. No files were modified.")
            self.worker.request_stop()
            self.worker.toggle_pause(False)  # release to exit cleanly
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.btn_start.setEnabled(True)
            self.lbl_stage.setText("🔄 Ready to Start")
            self.circular_progress.set_progress(0, "Ready")
        else:
            self.append_log("[UI] Starting merge stage...")
            self.worker.toggle_pause(False)
            self.lbl_stage.setText("⚙️ Merging Files")
            self.circular_progress.set_progress(0, "Merging...")

    def on_finished(self, report_csv, log_path):
        self.append_log("✨ === Processing Complete! ===")
        self.append_log(f"📊 Report: {report_csv}")
        self.append_log(f"📝 Log file: {log_path}")
        
        # Update UI to finished state
        self.circular_progress.set_progress(100, "Complete!")
        self.lbl_stage.setText("✨ All Done!")
        self.lbl_substage.setText("Processing completed successfully")
        
        # Reset buttons
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self.btn_pause.setText("⏸️ Pause")

    def on_fatal(self, msg):
        self.append_log(f"❌ FATAL ERROR: {msg}")
        QMessageBox.critical(self, "❌ Fatal Error", msg)
        
        # Reset UI to error state
        self.circular_progress.set_progress(0, "Error")
        self.lbl_stage.setText("❌ Error Occurred")
        self.lbl_substage.setText("Check log for details")
        
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)

def main():
    app = QApplication(sys.argv)
    
    # Set application properties
    app.setApplicationName("EasyTakeout")
    app.setApplicationDisplayName("🚀 EasyTakeout")
    app.setApplicationVersion("2.0.0")
    app.setOrganizationName("EasyTakeout")
    
    # Apply global application style
    app.setStyleSheet("""
        QToolTip {
            background-color: #2d2d2d;
            color: #ffffff;
            border: 1px solid #00d4aa;
            border-radius: 6px;
            padding: 8px;
            font-size: 12px;
        }
        
        QMessageBox {
            background-color: #1a1a1a;
            color: #ffffff;
        }
        
        QMessageBox QPushButton {
            background-color: #00d4aa;
            color: white;
            border: none;
            border-radius: 6px;
            padding: 8px 16px;
            font-weight: 600;
            min-width: 80px;
        }
        
        QMessageBox QPushButton:hover {
            background-color: #00e6c0;
        }
        
        QFileDialog {
            background-color: #1a1a1a;
            color: #ffffff;
        }
    """)
    
    w = App()
    w.show()
    
    # Center window on screen
    screen = app.primaryScreen().geometry()
    window = w.geometry()
    x = (screen.width() - window.width()) // 2
    y = (screen.height() - window.height()) // 2
    w.move(x, y)
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
