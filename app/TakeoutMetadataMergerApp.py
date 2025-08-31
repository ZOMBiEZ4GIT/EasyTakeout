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
import threading
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
                background-color: #1E1E1E;
                border-radius: 12px;
                border: 1px solid #2A2A2A;
                padding: 0px;
            }
            ModernCard:hover {
                border: 1px solid #4CAF50;
                background-color: #222222;
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
                    color: #4CAF50;
                    margin: 0px 0px 12px 0px;
                    padding-left: 4px;
                    letter-spacing: 0.5px;
                    background: transparent;
                    border: none;
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
                        stop:0 #4CAF50, stop:1 #2E7D32);
                    color: white;
                    border: none;
                    border-radius: 21px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 12px 24px;
                }
                ModernButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #66BB6A, stop:1 #4CAF50);
                    transform: translateY(-1px);
                }
                ModernButton:pressed {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #2E7D32, stop:1 #1B5E20);
                }
                ModernButton:disabled {
                    background: #333333;
                    color: #666666;
                }
            """)
        elif button_type == "secondary":
            self.setStyleSheet("""
                ModernButton {
                    background: #2196F3;
                    color: white;
                    border: none;
                    border-radius: 21px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 12px 24px;
                }
                ModernButton:hover {
                    background: #42A5F5;
                    transform: translateY(-1px);
                }
                ModernButton:pressed {
                    background: #1976D2;
                }
                ModernButton:disabled {
                    background: #333333;
                    color: #666666;
                }
            """)
        elif button_type == "danger":
            self.setStyleSheet("""
                ModernButton {
                    background: #F44336;
                    color: white;
                    border: none;
                    border-radius: 21px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 12px 24px;
                }
                ModernButton:hover {
                    background: #EF5350;
                    transform: translateY(-1px);
                }
                ModernButton:pressed {
                    background: #D32F2F;
                }
                ModernButton:disabled {
                    background: #333333;
                    color: #666666;
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
                background-color: #181818;
                border: 2px dashed #333333;
                border-radius: 12px;
                color: #aaaaaa;
            }
            ModernDropZone:hover {
                border-color: #4CAF50;
                background-color: rgba(76, 175, 80, 0.05);
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
        self.path_label.setStyleSheet("font-size: 12px; color: #4CAF50; margin-top: 5px;")
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
                    background-color: rgba(76, 175, 80, 0.1);
                    border: 2px solid #4CAF50;
                    border-radius: 12px;
                }
            """)
            
    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            ModernDropZone {
                background-color: #181818;
                border: 2px dashed #333333;
                border-radius: 12px;
            }
            ModernDropZone:hover {
                border-color: #4CAF50;
                background-color: rgba(76, 175, 80, 0.05);
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
        self.setMinimumSize(140, 140)
        self.progress = 0
        self.percentage_text = "0%"
        self.sublabel_text = ""
        self._progress_value = 0  # Initialize this first
        self.processing_rate = 0  # Files per second for color calculation
        self.dry_run_mode = False  # For blue theme
        
        # Animation for smooth progress updates
        self.progress_animation = QPropertyAnimation(self, b"progress_value")
        self.progress_animation.setDuration(300)
        self.progress_animation.setEasingCurve(QEasingCurve.OutCubic)
        
    # Property for animation
    def get_progress_value(self):
        return self._progress_value
        
    def set_progress_value(self, value):
        self._progress_value = value
        self.update()
        
    progress_value = Property(float, get_progress_value, set_progress_value)
        
    def set_progress(self, value, processed_count=0, total_count=0, rate=0, avg_rate=0, stage_elapsed=None):
        """Enhanced progress setter with average rate and stage timing"""
        new_progress = max(0, min(100, value))
        self.percentage_text = f"{int(new_progress)}%"
        
        # Enhanced sublabel with rate information
        if total_count > 0:
            rate_text = ""
            if rate > 0 or avg_rate > 0:
                if avg_rate > 0:
                    rate_text = f" • {rate:.1f}/{avg_rate:.1f} files/s"
                else:
                    rate_text = f" • {rate:.1f} files/s"
            
            stage_text = ""
            if stage_elapsed:
                mins, secs = divmod(int(stage_elapsed), 60)
                stage_text = f" • {mins:02d}:{secs:02d}"
            
            self.sublabel_text = f"{processed_count} of {total_count}{rate_text}{stage_text}"
        else:
            self.sublabel_text = ""
            
        # Store processing rate for color calculation (use current rate for stall detection)
        self.processing_rate = rate
        
        # Animate to new progress value
        self.progress_animation.stop()
        self.progress_animation.setStartValue(self._progress_value)
        self.progress_animation.setEndValue(new_progress)
        self.progress_animation.start()
        
        self.progress = new_progress
        
    def _get_progress_color(self):
        """Get color based on progress and processing speed"""
        if self.dry_run_mode:
            # Blue theme for dry run mode
            if self.processing_rate <= 0:
                return QColor("#2196F3")  # Default blue
            elif self.processing_rate > 2.0:  # Fast processing
                return QColor("#2196F3")  # Blue
            elif self.processing_rate > 0.5:  # Medium speed
                return QColor("#42A5F5")  # Light blue
            else:  # Slow processing
                return QColor("#1976D2")  # Dark blue
        else:
            # Neon-style progress colors
            if self.processing_rate <= 0:
                return QColor("#4CAF50")  # Completed: Green
            elif self.processing_rate > 2.0:  # Fast processing
                return QColor("#4CAF50")  # Green for good performance
            elif self.processing_rate > 0.5:  # Medium speed
                return QColor("#FFC107")  # Warnings: Orange
            else:  # Slow processing or failures
                return QColor("#F44336")  # Failures: Red
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Background circle
        painter.setPen(QPen(QColor("#333333"), 10))
        painter.drawEllipse(15, 15, 110, 110)
        
        # Progress arc with dynamic color
        progress_color = self._get_progress_color()
        painter.setPen(QPen(progress_color, 10))
        span_angle = int(self._progress_value * 360 / 100)
        painter.drawArc(15, 15, 110, 110, 90 * 16, -span_angle * 16)
        
        # Center percentage text
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 18, QFont.Bold))
        
        # Calculate text position for percentage
        text_rect = self.rect()
        text_rect.moveTop(text_rect.top() + 35)  # Move up slightly for percentage
        painter.drawText(text_rect, Qt.AlignCenter, self.percentage_text)
        
        # Sublabel text (X of Y)
        if self.sublabel_text:
            painter.setFont(QFont("Arial", 10, QFont.Normal))
            painter.setPen(QColor("#aaaaaa"))
            sublabel_rect = self.rect()
            sublabel_rect.moveTop(sublabel_rect.top() + 85)  # Position below percentage
            painter.drawText(sublabel_rect, Qt.AlignCenter, self.sublabel_text)

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
                background-color: #4CAF50;
                border-color: #4CAF50;
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
            color: #4CAF50;
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

class OutputFolderCard(ModernCard):
    def __init__(self, title, icon, path="", parent=None):
        super().__init__(parent=parent)
        self.folder_path = path
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(8)
        
        # Header with icon and title
        header_layout = QHBoxLayout()
        
        icon_label = QLabel(icon)
        icon_label.setStyleSheet("font-size: 20px;")
        icon_label.setFixedSize(30, 30)
        icon_label.setAlignment(Qt.AlignCenter)
        
        title_label = QLabel(title)
        title_label.setStyleSheet("""
            font-size: 14px;
            font-weight: 600;
            color: #ffffff;
        """)
        
        header_layout.addWidget(icon_label)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        # Count display
        self.count_label = QLabel("0")
        self.count_label.setStyleSheet("""
            font-size: 18px;
            font-weight: 700;
            color: #4CAF50;
            background-color: rgba(76, 175, 80, 0.1);
            border-radius: 12px;
            padding: 4px 8px;
            margin: 5px 0;
        """)
        self.count_label.setAlignment(Qt.AlignCenter)
        
        # Open button
        self.open_btn = ModernButton("📂 Open Folder", "secondary")
        self.open_btn.setMaximumHeight(35)
        self.open_btn.clicked.connect(self.open_folder)
        self.open_btn.setEnabled(False)  # Disabled until path is set
        
        layout.addLayout(header_layout)
        layout.addWidget(self.count_label)
        layout.addWidget(self.open_btn)
        
        self.setLayout(layout)
        self.setFixedHeight(120)
        
        # Add hover effect
        self.setStyleSheet("""
            OutputFolderCard {
                background-color: #1E1E1E;
                border-radius: 12px;
                border: 1px solid #2A2A2A;
            }
            OutputFolderCard:hover {
                border: 1px solid #4CAF50;
                background-color: #222222;
                transform: translateY(-2px);
            }
        """)
        
    def set_path(self, path: str):
        """Set the folder path and enable the open button"""
        self.folder_path = path
        self.open_btn.setEnabled(bool(path))
        
    def update_count(self, count: int):
        """Update the count display"""
        self.count_label.setText(str(count))
        
        # Add visual feedback for non-zero counts
        if count > 0:
            self.count_label.setStyleSheet("""
                font-size: 18px;
                font-weight: 700;
                color: #ffffff;
                background-color: #4CAF50;
                border-radius: 12px;
                padding: 4px 8px;
                margin: 5px 0;
            """)
        else:
            self.count_label.setStyleSheet("""
                font-size: 18px;
                font-weight: 700;
                color: #4CAF50;
                background-color: rgba(76, 175, 80, 0.1);
                border-radius: 12px;
                padding: 4px 8px;
                margin: 5px 0;
            """)
    
    def open_folder(self):
        """Open the folder in the system file explorer"""
        if self.folder_path and Path(self.folder_path).exists():
            import subprocess
            import sys
            
            try:
                if sys.platform == "win32":
                    subprocess.run(["explorer", str(Path(self.folder_path))], check=True)
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(Path(self.folder_path))], check=True)
                else:
                    subprocess.run(["xdg-open", str(Path(self.folder_path))], check=True)
            except Exception as e:
                # Fallback: show message with path
                QMessageBox.information(self, "Folder Path", 
                                      f"Folder location:\n{self.folder_path}")
        else:
            QMessageBox.warning(self, "Folder Not Found", 
                              "The output folder doesn't exist yet or path is not set.")

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
    failure_summary = Signal(dict)            # failure reason counts
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
        # enhanced tracking
        self.stage_start_time = None
        self.rate_history = []  # For calculating average rate
        self.current_stage = "Ready"
        # failure tracking
        self.failure_reasons = {
            "no_json": 0,
            "bad_json": 0,
            "exiftool_error": 0,
            "partner_error": 0,
            "other_error": 0
        }

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
            if self._paused:
                self.log(f"DEBUG: _maybe_pause - paused={self._paused}, stop={self._stop}")
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

    def _start_stage(self, stage_name: str):
        """Start tracking a new stage"""
        self.current_stage = stage_name
        self.stage_start_time = time.time()
        self.rate_history.clear()
        self.log(f"Starting stage: {stage_name}")

    def _calculate_average_rate(self, current_rate: float) -> float:
        """Calculate rolling average rate"""
        self.rate_history.append(current_rate)
        # Keep only last 10 measurements for rolling average
        if len(self.rate_history) > 10:
            self.rate_history.pop(0)
        return sum(self.rate_history) / len(self.rate_history) if self.rate_history else 0

    def _get_stage_elapsed(self) -> Optional[float]:
        """Get elapsed time for current stage"""
        if self.stage_start_time:
            return time.time() - self.stage_start_time
        return None

    # -------- Stage 1: Plan (three-pass with explicit subfolder inventory) --------
    def stage_plan(self):
        self._start_stage("Planning")
        self.stage.emit("Planning…")
        src = self.source
        if not src.exists():
            raise RuntimeError("Source folder does not exist.")
        
        # Additional validation
        if not src.is_dir():
            raise RuntimeError(f"Source path is not a directory: {src}")
        
        # Test basic access
        try:
            list(src.iterdir())
            self.log(f"DEBUG: Successfully accessed source directory contents")
        except PermissionError as e:
            raise RuntimeError(f"Permission denied accessing source directory: {e}")
        except Exception as e:
            raise RuntimeError(f"Cannot access source directory: {e}")

        # Pass 0: Inventory all subdirectories first
        self.log("=== STARTING COMPREHENSIVE SUBFOLDER INVENTORY ===")
        self.log(f"Source directory: {src}")
        self.log("This will recursively discover ALL subdirectories before processing...")
        self.substage.emit("Inventorying all subdirectories...")
        
        all_directories = []
        total_dirs = 0
        last_ping = time.time()
        
        try:
            self.log(f"DEBUG: Starting directory enumeration on: {src}")
            self.log(f"DEBUG: Source exists: {src.exists()}")
            self.log(f"DEBUG: Source is directory: {src.is_dir()}")
            
            # Use iterative approach instead of os.walk to avoid hanging
            self.log("DEBUG: Initializing directory scan variables...")
            directories_to_scan = [src]
            scanned_count = 0
            t0 = time.time()  # Start timing
            
            self.log(f"DEBUG: Created directory queue with {len(directories_to_scan)} items")
            self.log("DEBUG: Starting iterative directory scan...")
            self.log(f"DEBUG: About to enter while loop. _stop={self._stop}")
            
            while directories_to_scan and not self._stop:
                self.log(f"DEBUG: Loop iteration {total_dirs + 1}, queue size: {len(directories_to_scan)}")
                self.log("DEBUG: About to call _maybe_pause()")
                self._maybe_pause()
                
                self.log("DEBUG: About to pop directory from queue")
                current_dir = directories_to_scan.pop(0)
                self.log(f"DEBUG: Popped directory: {current_dir}")
                
                all_directories.append(current_dir)
                total_dirs += 1
                scanned_count += 1
                
                # Log progress for first few and periodically
                if total_dirs <= 10 or total_dirs % 100 == 0:
                    self.log(f"DEBUG: Scanning directory #{total_dirs}: {current_dir}")
                
                try:
                    # Get subdirectories with better error handling for large dirs
                    self.log(f"DEBUG: About to scan contents of: {current_dir}")
                    subdirs = []
                    item_count = 0
                    
                    try:
                        # Use os.listdir instead of iterdir for better performance on large dirs
                        self.log(f"DEBUG: Using os.listdir() on: {current_dir}")
                        dir_contents = os.listdir(str(current_dir))
                        item_count_total = len(dir_contents)
                        self.log(f"DEBUG: Found {item_count_total} items in: {current_dir}")
                        
                        # Warn about very large directories
                        if item_count_total > 10000:
                            self.log(f"WARNING: Very large directory detected ({item_count_total} items): {current_dir}")
                            self.log("This may take several minutes to process...")
                        elif item_count_total > 1000:
                            self.log(f"INFO: Large directory detected ({item_count_total} items): {current_dir}")
                        
                        # Process in chunks to avoid blocking UI
                        chunk_size = 1000  # Process 1000 items at a time
                        for i in range(0, len(dir_contents), chunk_size):
                            if self._stop: break
                            self._maybe_pause()
                            
                            chunk = dir_contents[i:i + chunk_size]
                            for item_name in chunk:
                                if self._stop: break
                                
                                item_path = current_dir / item_name
                                item_count += 1
                                
                                # Update UI every 100 items for very large directories
                                if item_count % 100 == 0:
                                    self.log(f"DEBUG: Processed {item_count}/{len(dir_contents)} items in {current_dir}")
                                
                                try:
                                    if item_path.is_dir():
                                        subdirs.append(item_path)
                                except (OSError, PermissionError):
                                    # Skip items we can't access
                                    continue
                                    
                        self.log(f"DEBUG: Found {len(subdirs)} subdirectories in: {current_dir}")
                        
                    except PermissionError:
                        self.log(f"DEBUG: Permission denied listing contents of: {current_dir}")
                    except Exception as e:
                        self.log(f"DEBUG: Error listing contents of {current_dir}: {e}")
                            
                    # Add subdirs to queue
                    directories_to_scan.extend(subdirs)
                    
                    # Update UI more frequently for large scans
                    now = time.time()
                    if now - last_ping > 0.3:  # Update every 300ms
                        rel_path = current_dir.relative_to(src) if current_dir != src else "."
                        queue_size = len(directories_to_scan)
                        elapsed = now - t0
                        rate = total_dirs / max(elapsed, 1)
                        self.substage.emit(f"Found {total_dirs} directories | Queue: {queue_size} | {rate:.1f} dirs/s | Current: {rel_path}")
                        
                        # Show rough progress in progress bar during scanning
                        # Estimate progress based on queue size reduction
                        if total_dirs > 10:
                            estimated_progress = min(50, int((total_dirs / max(total_dirs + queue_size, 1)) * 50))
                            self.progress.emit(estimated_progress, 100)
                        
                        last_ping = now
                        
                except PermissionError:
                    self.log(f"DEBUG: Permission denied on: {current_dir}")
                    continue
                except Exception as e:
                    self.log(f"DEBUG: Error scanning {current_dir}: {e}")
                    continue
                    
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
            # Pause here to wait for user confirmation
            self.toggle_pause(True)
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
                        avg_rate = self._calculate_average_rate(rate)
                        stage_elapsed = self._get_stage_elapsed()
                        remain = total_media - processed
                        eta_sec = int(remain / max(rate, 1e-6))
                        eta_m, eta_s = eta_sec // 60, eta_sec % 60
                        
                        rel_path = directory.relative_to(src) if directory != src else "."
                        dir_progress = int((dirs_processed / total_dirs) * 100) if total_dirs > 0 else 0
                        
                        self.progress.emit(processed, total_media)
                        self.substage.emit(
                            f"Mapping JSON… {processed}/{total_media} ({dir_progress}% dirs)  |  "
                            f"Current: {rate:.1f} files/s  |  Avg: {avg_rate:.1f} files/s  |  ETA {eta_m}m {eta_s}s  |  in {rel_path}"
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
        # Pause here to wait for user confirmation before proceeding to merge stage
        self.toggle_pause(True)

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
        self._start_stage("Merging")
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

                # progress subline (ETA) with enhanced metrics
                rate = idx / max(time.time() - t0, 1e-6)
                avg_rate = self._calculate_average_rate(rate)
                stage_elapsed = self._get_stage_elapsed()
                remain = total - idx
                eta = int(remain / max(rate, 1e-6))
                
                stage_time_str = ""
                if stage_elapsed:
                    mins, secs = divmod(int(stage_elapsed), 60)
                    stage_time_str = f"  |  Stage: {mins:02d}:{secs:02d}"
                
                self.substage.emit(f"Merging… {idx}/{total}  |  Current: {rate:.1f} files/s  |  Avg: {avg_rate:.1f} files/s  |  ETA {eta//60}m {eta%60}s{stage_time_str}")

                if sidecar is None or not sidecar.exists():
                    msg = "No matching JSON sidecar"
                    self.log(f"FAIL (no JSON): {media}")
                    writer.writerow([str(media), "", "FAILED", msg])
                    self.move_pair(media, None, ok=False)
                    fail_ct += 1
                    self.failure_reasons["no_json"] += 1
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
                    self.failure_reasons["bad_json"] += 1
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
                    self.failure_reasons["exiftool_error"] += 1
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
                        self.failure_reasons["partner_error"] += 1

                self.log(f"OK: {media}")
                writer.writerow([str(media), str(sidecar),
                                 "COMPLETED" if not partner_msg else "COMPLETED_WITH_PARTNER_WARN",
                                 partner_msg])
                self.move_pair(media, sidecar, ok=True)
                ok_ct += 1
                self.counts.emit(ok_ct, fail_ct, warn_ct)
                self.progress.emit(idx, total)

        # Emit failure summary
        self.failure_summary.emit(self.failure_reasons.copy())
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
        self.is_dry_run_mode = False
        self._setup_modern_theme()
        self._build_ui()
        
    def _setup_modern_theme(self):
        """Apply the modern dark theme to the entire application"""
        self.setStyleSheet("""
            QWidget {
                background-color: #121212;
                color: #ffffff;
                font-family: 'Segoe UI', 'Inter', 'Arial', sans-serif;
                font-size: 13px;
            }
            
            QMainWindow {
                background-color: #121212;
            }
            
            QLabel {
                color: #ffffff;
                background: transparent;
            }
            
            QLineEdit {
                background-color: #1E1E1E;
                border: 1px solid #2A2A2A;
                border-radius: 10px;
                padding: 12px 16px;
                font-size: 14px;
                color: #ffffff;
                selection-background-color: #4CAF50;
            }
            
            QLineEdit:focus {
                border-color: #4CAF50;
                background-color: #222222;
                box-shadow: 0 0 8px rgba(76, 175, 80, 0.3);
            }
            
            QLineEdit::placeholder {
                color: #666666;
                font-style: italic;
            }
            
            QTextEdit {
                background-color: #181818;
                border: 1px solid #2A2A2A;
                border-radius: 10px;
                padding: 12px;
                font-family: 'Consolas', 'Monaco', 'JetBrains Mono', monospace;
                font-size: 12px;
                line-height: 1.5;
                color: #ffffff;
            }
            
            QTextEdit::placeholder {
                color: #666666;
                font-style: italic;
            }
            
            QProgressBar {
                background-color: #333333;
                border: none;
                border-radius: 10px;
                height: 8px;
                text-align: center;
                color: #ffffff;
            }
            
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4CAF50, stop:1 #2E7D32);
                border-radius: 10px;
            }
            
            QScrollBar:vertical {
                background-color: #1E1E1E;
                width: 12px;
                border-radius: 6px;
            }
            
            QScrollBar::handle:vertical {
                background-color: #333333;
                border-radius: 6px;
                min-height: 20px;
            }
            
            QScrollBar::handle:vertical:hover {
                background-color: #4CAF50;
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
        
        # Check if retry button should be enabled
        self._check_enable_retry_button()
        
        # Animate the dropzone to show success
        self.source_dropzone.setStyleSheet("""
            ModernDropZone {
                background-color: rgba(76, 175, 80, 0.15);
                border: 2px solid #4CAF50;
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
                border-color: #4CAF50;
                background-color: rgba(76, 175, 80, 0.05);
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
        
        # Check if we're actually setting the defaults
        auto_created = False
        if should_set(self.inp_completed): 
            self.inp_completed.setText(str(comp))
            auto_created = True
        if should_set(self.inp_failed):    
            self.inp_failed.setText(str(fail))
            auto_created = True
        if should_set(self.inp_logs):      
            self.inp_logs.setText(str(logs))
            auto_created = True
        
        # Show visual confirmation if we auto-created the structure
        if auto_created:
            self._show_smart_defaults_feedback(str(results))

    def _show_smart_defaults_feedback(self, results_path: str):
        """Show visual confirmation that output structure was auto-created"""
        # Add a temporary notification to the log
        self.append_log("✅ Auto-created output structure: EasyTakeout-Results")
        
        # Create a subtle notification that fades after a few seconds
        if hasattr(self, 'dest_card'):
            # Add a temporary checkmark and message to the destination card
            if not hasattr(self, 'smart_feedback_label'):
                self.smart_feedback_label = QLabel("✅ Auto-created output structure")
                self.smart_feedback_label.setStyleSheet("""
                    QLabel {
                        color: #4CAF50;
                        font-size: 12px;
                        font-weight: 600;
                        background-color: rgba(76, 175, 80, 0.1);
                        border-radius: 4px;
                        padding: 4px 8px;
                        margin-top: 5px;
                    }
                """)
                self.smart_feedback_label.setAlignment(Qt.AlignCenter)
                self.dest_card.layout.addWidget(self.smart_feedback_label)
                
                # Auto-hide after 5 seconds
                QTimer.singleShot(5000, lambda: self.smart_feedback_label.hide())

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
            color: #4CAF50;
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
        self.dest_card = ModernCard("🎯 Output Folders")
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
        
        self.dest_card.layout.addLayout(dest_grid)
        left_panel.addWidget(self.dest_card)

        # Options card
        options_card = ModernCard("⚙️ Processing Options")
        options_layout = QVBoxLayout()
        
        # Profile presets dropdown
        preset_layout = QVBoxLayout()
        preset_layout.setSpacing(8)
        
        preset_label = QLabel("🎛️ Profile Presets:")
        preset_label.setStyleSheet("color: #ffffff; font-weight: 600;")
        
        from PySide6.QtWidgets import QComboBox
        self.preset_combo = QComboBox()
        self.preset_combo.setStyleSheet("""
            QComboBox {
                background-color: #1E1E1E;
                border: 1px solid #2A2A2A;
                border-radius: 10px;
                padding: 8px 12px;
                font-size: 14px;
                color: #ffffff;
                min-height: 20px;
            }
            QComboBox:hover {
                border-color: #4CAF50;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox::down-arrow {
                image: none;
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #1E1E1E;
                border: 1px solid #2A2A2A;
                border-radius: 8px;
                color: #ffffff;
                selection-background-color: #4CAF50;
                padding: 4px;
            }
        """)
        
        # Add preset options
        self.preset_combo.addItem("🟢 Standard (overwrite EXIF)")
        self.preset_combo.addItem("🟡 Safe (don't overwrite, only add missing)")
        self.preset_combo.addItem("🔵 Debug (dry run + full logs)")
        self.preset_combo.addItem("⚙️ Custom")
        
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        self.preset_combo.setToolTip("""Select a processing profile:
🟢 Standard: Recommended for most users - overwrites existing metadata
🟡 Safe: Conservative approach - only adds missing metadata
🔵 Debug: Preview mode with detailed logging - no files modified
⚙️ Custom: Manual control over all settings""")
        
        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.preset_combo)
        options_layout.addLayout(preset_layout)
        
        # Add spacing
        options_layout.addSpacing(10)
        
        # Toggles
        self.chk_preserve = ModernToggle("Preserve folder structure")
        self.chk_preserve.setChecked(True)
        self.chk_preserve.stateChanged.connect(self._on_manual_option_change)
        
        self.chk_overwrite = ModernToggle("Overwrite existing metadata")
        self.chk_overwrite.setChecked(True)
        self.chk_overwrite.stateChanged.connect(self._on_manual_option_change)
        
        self.chk_dryrun = ModernToggle("Dry run (preview only)")
        self.chk_dryrun.stateChanged.connect(self._on_dry_run_toggle)
        self.chk_dryrun.stateChanged.connect(self._on_manual_option_change)
        
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
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(10)
        
        # Main controls row
        main_controls = QHBoxLayout()
        main_controls.setSpacing(15)
        
        self.btn_start = ModernButton("▶️ Start Processing", "primary")
        self.btn_pause = ModernButton("⏸️ Pause", "secondary")
        self.btn_stop = ModernButton("⏹️ Stop", "danger")
        
        # Enhanced visual states for pause button
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self._update_pause_button_style(enabled=False)
        
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(self.on_pause_toggle)
        self.btn_stop.clicked.connect(self.on_stop)
        
        main_controls.addWidget(self.btn_start)
        main_controls.addWidget(self.btn_pause)
        main_controls.addWidget(self.btn_stop)
        
        # Retry failed button
        self.btn_retry_failed = ModernButton("🔄 Retry Only Failed", "secondary")
        self.btn_retry_failed.setEnabled(False)  # Disabled until failed folder exists
        self.btn_retry_failed.clicked.connect(self.on_retry_failed)
        
        controls_layout.addLayout(main_controls)
        controls_layout.addWidget(self.btn_retry_failed)
        
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
            color: #4CAF50;
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
        
        # Dry run banner (initially hidden)
        self.dry_run_banner = ModernCard()
        self.dry_run_banner.setStyleSheet("""
            ModernCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(54, 162, 235, 0.15), stop:1 rgba(54, 162, 235, 0.05));
                border: 2px solid #36a2eb;
                border-radius: 12px;
            }
        """)
        banner_layout = QHBoxLayout()
        banner_layout.setContentsMargins(15, 10, 15, 10)
        
        banner_icon = QLabel("🔍")
        banner_icon.setStyleSheet("font-size: 24px;")
        
        banner_text = QLabel("DRY RUN MODE — No changes will be made to your files")
        banner_text.setStyleSheet("""
            font-size: 14px;
            font-weight: 600;
            color: #36a2eb;
        """)
        
        banner_layout.addWidget(banner_icon)
        banner_layout.addWidget(banner_text)
        banner_layout.addStretch()
        
        self.dry_run_banner.layout.addLayout(banner_layout)
        self.dry_run_banner.hide()  # Initially hidden
        right_panel.addWidget(self.dry_run_banner)
        
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
        
        # Output folder cards
        output_cards_layout = QHBoxLayout()
        output_cards_layout.setSpacing(15)
        
        self.completed_folder_card = OutputFolderCard("Completed Files", "✅")
        self.failed_folder_card = OutputFolderCard("Failed Files", "❌")
        self.logs_folder_card = OutputFolderCard("Logs & Reports", "📝")
        
        output_cards_layout.addWidget(self.completed_folder_card)
        output_cards_layout.addWidget(self.failed_folder_card)
        output_cards_layout.addWidget(self.logs_folder_card)
        
        right_panel.addLayout(output_cards_layout)

        # Now Processing card
        processing_card = ModernCard("⚡ Live Processing")
        processing_layout = QVBoxLayout()
        
        # Thumbnail area
        thumb_container = QFrame()
        thumb_container.setStyleSheet("""
            QFrame {
                background: #181818;
                border-radius: 12px;
                border: 1px solid #2A2A2A;
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

        # Collapsible detailed log window
        log_card = ModernCard()
        log_layout = QVBoxLayout()
        
        # Log header with toggle button
        log_header = QHBoxLayout()
        log_title = QLabel("📜 Detailed Log")
        log_title.setStyleSheet("""
            font-size: 16px;
            font-weight: 600;
            color: #ffffff;
        """)
        
        self.log_toggle_btn = ModernButton("Hide Details", "secondary")
        self.log_toggle_btn.setMaximumWidth(120)
        self.log_toggle_btn.clicked.connect(self.toggle_detailed_log)
        
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(self.log_toggle_btn)
        
        # Log text area
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Detailed processing log will appear here...")
        self.log.setMinimumHeight(200)
        
        # Start with log collapsed by default
        self.log.hide()
        self.log_toggle_btn.setText("Show Details")
        
        log_layout.addLayout(log_header)
        log_layout.addWidget(self.log)
        
        log_card.layout.addLayout(log_layout)
        right_panel.addWidget(log_card, 1)  # This gets most of the space
        
        # Tip
        tip_card = ModernCard()
        tip = QLabel("💡 <b>Pro Tip:</b> You can set Source = Failed folder to retry only failed files later. Use drag & drop for fastest folder selection!")
        tip.setStyleSheet("""
            color: #aaaaaa;
            font-size: 12px;
            padding: 8px;
            background-color: rgba(76, 175, 80, 0.05);
            border-radius: 6px;
            border-left: 3px solid #4CAF50;
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

    def _on_dry_run_toggle(self, state):
        """Handle dry run mode toggle"""
        self.is_dry_run_mode = bool(state)
        if self.is_dry_run_mode:
            self._apply_dry_run_theme()
            self.dry_run_banner.show()
            self.append_log("🔍 Dry run mode enabled - No files will be modified")
        else:
            self._apply_normal_theme()
            self.dry_run_banner.hide()
            self.append_log("💾 Dry run mode disabled - Files will be modified normally")

    def _on_preset_changed(self, preset_text: str):
        """Handle preset selection changes"""
        # Temporarily disconnect manual change signals to avoid recursion
        self.chk_preserve.stateChanged.disconnect()
        self.chk_overwrite.stateChanged.disconnect()
        self.chk_dryrun.stateChanged.disconnect()
        
        try:
            if preset_text.startswith("🟢 Standard"):
                # Standard: overwrite EXIF, preserve structure, no dry run
                self.chk_preserve.setChecked(True)
                self.chk_overwrite.setChecked(True)
                self.chk_dryrun.setChecked(False)
                self.append_log("🟢 Applied Standard preset: overwrite EXIF enabled")
                
            elif preset_text.startswith("🟡 Safe"):
                # Safe: don't overwrite, preserve structure, no dry run
                self.chk_preserve.setChecked(True)
                self.chk_overwrite.setChecked(False)
                self.chk_dryrun.setChecked(False)
                self.append_log("🟡 Applied Safe preset: only add missing metadata")
                
            elif preset_text.startswith("🔵 Debug"):
                # Debug: dry run mode, preserve structure, don't overwrite
                self.chk_preserve.setChecked(True)
                self.chk_overwrite.setChecked(False)
                self.chk_dryrun.setChecked(True)
                self.append_log("🔵 Applied Debug preset: dry run mode with full logging")
                
            elif preset_text.startswith("⚙️ Custom"):
                # Custom: don't change anything, user controls all settings
                self.append_log("⚙️ Custom preset selected: manual control enabled")
        
        finally:
            # Reconnect signals
            self.chk_preserve.stateChanged.connect(self._on_manual_option_change)
            self.chk_overwrite.stateChanged.connect(self._on_manual_option_change)
            self.chk_dryrun.stateChanged.connect(self._on_dry_run_toggle)
            self.chk_dryrun.stateChanged.connect(self._on_manual_option_change)
            
            # Trigger dry run toggle if needed
            self._on_dry_run_toggle(self.chk_dryrun.isChecked())

    def _on_manual_option_change(self):
        """Handle manual changes to options - switch to Custom preset"""
        # Check if current settings match any preset
        preserve = self.chk_preserve.isChecked()
        overwrite = self.chk_overwrite.isChecked()
        dryrun = self.chk_dryrun.isChecked()
        
        # Temporarily disconnect to avoid recursion
        self.preset_combo.currentTextChanged.disconnect()
        
        try:
            if preserve and overwrite and not dryrun:
                self.preset_combo.setCurrentText("🟢 Standard (overwrite EXIF)")
            elif preserve and not overwrite and not dryrun:
                self.preset_combo.setCurrentText("🟡 Safe (don't overwrite, only add missing)")
            elif preserve and not overwrite and dryrun:
                self.preset_combo.setCurrentText("🔵 Debug (dry run + full logs)")
            else:
                self.preset_combo.setCurrentText("⚙️ Custom")
        finally:
            # Reconnect signal
            self.preset_combo.currentTextChanged.connect(self._on_preset_changed)

    def _apply_dry_run_theme(self):
        """Apply blue theme for dry run mode"""
        # Set dry run mode on circular progress
        self.circular_progress.dry_run_mode = True
        
        # Update progress card with blue theme
        self.circular_progress.setStyleSheet("""
            CircularProgress {
                background-color: rgba(54, 162, 235, 0.1);
                border-radius: 12px;
            }
        """)
        
        # Update stage label with blue color
        self.lbl_stage.setStyleSheet("""
            QLabel {
                font-size: 20px;
                font-weight: 600;
                color: #36a2eb;
                margin-bottom: 10px;
            }
        """)
        
        # Update start button to show preview mode
        self.btn_start.setText("🔍 Start Preview")
        self.btn_start.setStyleSheet("""
            ModernButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #36a2eb, stop:1 #2c8bd9);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 12px 24px;
            }
            ModernButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4db8ff, stop:1 #36a2eb);
            }
            ModernButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2c8bd9, stop:1 #1f6bb3);
            }
            ModernButton:disabled {
                background: #555555;
                color: #999999;
            }
        """)

    def _apply_normal_theme(self):
        """Apply normal green theme"""
        # Reset dry run mode on circular progress
        self.circular_progress.dry_run_mode = False
        
        # Reset progress card
        self.circular_progress.setStyleSheet("")
        
        # Reset stage label
        self.lbl_stage.setStyleSheet("""
            QLabel {
                font-size: 20px;
                font-weight: 600;
                color: #4CAF50;
                margin-bottom: 10px;
            }
        """)
        
        # Reset start button
        self.btn_start.setText("▶️ Start Processing")
        self.btn_start.setStyleSheet("""
            ModernButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4CAF50, stop:1 #2E7D32);
                color: white;
                border: none;
                border-radius: 21px;
                font-size: 14px;
                font-weight: 600;
                padding: 12px 24px;
            }
            ModernButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #66BB6A, stop:1 #4CAF50);
                transform: translateY(-1px);
            }
            ModernButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2E7D32, stop:1 #1B5E20);
            }
            ModernButton:disabled {
                background: #333333;
                color: #666666;
            }
        """)

    def _maybe_autofill_from_text(self):
        t = self.inp_source.text().strip()
        if t: self._autofill_results_from_source(t)

    # ---- UI helpers ----
    def set_thumb(self, path: str, caption: str):
        """Enhanced thumbnail display with file information"""
        # Extract filename from caption for file info lookup
        filename = caption or "Ready to process files"
        
        # Get file info if path exists
        file_info = ""
        if path and Path(path).exists():
            try:
                # Get original media file path from thumbnail path
                # Thumbnail names are hashed, so we'll get info from the caption filename
                if caption and caption != "Ready to process files":
                    # Look for the actual file in the source directory if available
                    source_path = None
                    if hasattr(self, 'worker') and self.worker and hasattr(self.worker, 'source'):
                        # Try to find the actual file
                        for media_file in Path(self.worker.source).rglob(caption):
                            if media_file.is_file():
                                source_path = media_file
                                break
                    
                    if source_path and source_path.exists():
                        # Get file size
                        size_bytes = source_path.stat().st_size
                        if size_bytes < 1024:
                            size_str = f"{size_bytes} B"
                        elif size_bytes < 1024 * 1024:
                            size_str = f"{size_bytes / 1024:.1f} KB"
                        else:
                            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                        
                        # Get dimensions if it's an image
                        dimensions_str = ""
                        if is_image_file(source_path):
                            try:
                                from PIL import Image
                                with Image.open(source_path) as img:
                                    dimensions_str = f"{img.width}×{img.height}"
                            except:
                                dimensions_str = "Unknown size"
                        elif is_video_file(source_path):
                            # Try to get video dimensions with ffprobe if available
                            try:
                                if hasattr(self, 'worker') and self.worker and self.worker.ffmpeg:
                                    import subprocess
                                    ffprobe_cmd = [
                                        self.worker.ffmpeg.replace('ffmpeg', 'ffprobe'),
                                        '-v', 'quiet', '-print_format', 'json',
                                        '-show_streams', str(source_path)
                                    ]
                                    result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=5)
                                    if result.returncode == 0:
                                        import json
                                        data = json.loads(result.stdout)
                                        for stream in data.get('streams', []):
                                            if stream.get('codec_type') == 'video':
                                                w, h = stream.get('width', 0), stream.get('height', 0)
                                                if w and h:
                                                    dimensions_str = f"{w}×{h}"
                                                break
                            except:
                                pass
                            if not dimensions_str:
                                dimensions_str = "Video"
                        
                        # Combine dimensions and size
                        if dimensions_str:
                            file_info = f"{dimensions_str} · {size_str}"
                        else:
                            file_info = size_str
                            
            except Exception as e:
                file_info = ""
        
        # Update caption with file info
        display_caption = filename
        if file_info:
            display_caption = f"{filename}\n{file_info}"
        
        self.thumb_caption.setText(display_caption)
        
        if path and Path(path).exists():
            pm = QPixmap(path)
            if not pm.isNull():
                # Scale to fit the container nicely
                scaled_pm = pm.scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb.setPixmap(scaled_pm)
                
                # Determine if this is an image or video based on caption
                is_video = any(ext in caption.lower() for ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v']) if caption else False
                
                if is_video:
                    # Purple glow for video thumbnails
                    self.thumb.setStyleSheet("""
                        background: transparent;
                        border: 2px solid #9C27B0;
                        border-radius: 8px;
                        padding: 4px;
                    """)
                else:
                    # Cyan glow for image thumbnails  
                    self.thumb.setStyleSheet("""
                        background: transparent;
                        border: 2px solid #00BCD4;
                        border-radius: 8px;
                        padding: 4px;
                    """)
                return
        
        # Fallback icons with better styling
        is_video = any(ext in caption.lower() for ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v']) if caption else False
        
        if is_video:
            self.thumb.setText("🎬")
            self.thumb.setStyleSheet("""
                font-size: 48px;
                color: #9C27B0;
                background: transparent;
                border: 2px solid #9C27B0;
                border-radius: 12px;
                padding: 12px;
            """)
        else:
            self.thumb.setText("🎯")
            self.thumb.setStyleSheet("""
                font-size: 48px;
                color: #00BCD4;
                background: transparent;
                border: none;
            """)

    def append_log(self, line: str):
        """Enhanced log append with color coding"""
        # Color code the log line based on content
        colored_line = self._colorize_log_line(line)
        
        self.log.append(colored_line)
        
        # Also add to activity stream with simpler formatting for casual viewing
        activity_line = self._simplify_for_activity(line)
        self.activity_stream.append(activity_line)
        
        # Auto-scroll to bottom with smooth animation
        QTimer.singleShot(50, lambda: self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum()))
        QTimer.singleShot(50, lambda: self.activity_stream.verticalScrollBar().setValue(
            self.activity_stream.verticalScrollBar().maximum()))
    
    def _colorize_log_line(self, line: str) -> str:
        """Add HTML color coding and icons to log lines based on content"""
        line_lower = line.lower()
        
        # Apply monospace font and add icons
        if any(word in line_lower for word in ['error', 'fail', 'fatal', '❌']):
            if not line.startswith('❌'):
                line = f"❌ {line}"
            return f'<span style="font-family: Consolas, Monaco, monospace; color: #F44336; font-weight: 500;">{line}</span>'
        elif any(word in line_lower for word in ['warn', 'warning', '⚠️']):
            if not line.startswith('⚠️'):
                line = f"⚠️ {line}"
            return f'<span style="font-family: Consolas, Monaco, monospace; color: #FFC107; font-weight: 500;">{line}</span>'
        elif any(word in line_lower for word in ['ok:', 'completed', 'success', '✅', '✨']):
            if not line.startswith('✅') and not line.startswith('✨'):
                line = f"✅ {line}"
            return f'<span style="font-family: Consolas, Monaco, monospace; color: #4CAF50; font-weight: 500;">{line}</span>'
        elif line.startswith('DEBUG:'):
            return f'<span style="font-family: Consolas, Monaco, monospace; color: #666666; font-style: italic;">{line}</span>'
        else:
            return f'<span style="font-family: Consolas, Monaco, monospace; color: #ffffff;">{line}</span>'
    
    def _simplify_for_activity(self, line: str) -> str:
        """Simplify log lines for the activity stream"""
        # Remove timestamp for cleaner activity view
        if line.startswith('[') and ']' in line:
            # Extract just the message part after timestamp
            parts = line.split(']', 1)
            if len(parts) > 1:
                simplified = parts[1].strip()
                return self._colorize_log_line(simplified)
        return self._colorize_log_line(line)

    def set_counts(self, ok, fail, warn): 
        self.stats_completed.update_value(ok)
        self.stats_failed.update_value(fail)
        self.stats_warnings.update_value(warn)
        
        # Also update output folder cards
        self.completed_folder_card.update_count(ok)
        self.failed_folder_card.update_count(fail)
        
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
        
        # Update output folder cards with paths
        self.completed_folder_card.set_path(ok)
        self.failed_folder_card.set_path(fl)
        self.logs_folder_card.set_path(lg)

        # Clear UI
        self.log.clear(); self.activity_stream.clear()
        self.prog.setValue(0); self.set_counts(0,0,0); self.set_remaining(0,0)
        self.circular_progress.set_progress(0, 0, 0, 0)
        
        # Update stage
        self.lbl_stage.setText("🗒️ Planning Stage")
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setText("⏸️ Pause")
        self._update_pause_button_style(enabled=True)

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
        self.worker.failure_summary.connect(self.on_failure_summary)
        # Start normally - pausing will happen automatically after planning completes
        self.worker.start()

    def on_pause_toggle(self):
        if not self.worker: return
        if self.btn_pause.text().startswith("⏸️"):
            self.worker.toggle_pause(True)
            self.btn_pause.setText("▶️ Resume")
            self.lbl_stage.setText("⏸️ Paused")
            self.append_log("[UI] Processing paused")
            # Update button style to resume state
            self.btn_pause.setStyleSheet("""
                ModernButton {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #4CAF50, stop:1 #45a049);
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 12px 24px;
                }
                ModernButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #66BB6A, stop:1 #4CAF50);
                }
            """)
        else:
            self.worker.toggle_pause(False)
            self.btn_pause.setText("⏸️ Pause")
            self.lbl_stage.setText("▶️ Resuming...")
            self.append_log("[UI] Processing resumed")
            # Restore normal pause button style
            self._update_pause_button_style(enabled=True)

    def on_stop(self):
        if self.worker:
            self.worker.request_stop()
            self.append_log("[UI] Stop requested - finishing current operation...")
            self.lbl_stage.setText("⏹️ Stopping...")
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self._update_pause_button_style(enabled=False)

    def on_retry_failed(self):
        """Set source to failed folder and start retry processing"""
        failed_path = self.inp_failed.text().strip()
        if not failed_path:
            QMessageBox.warning(self, "⚠️ No Failed Folder", 
                               "No failed folder path is set. Please run a process first to generate failed files.")
            return
        
        failed_path_obj = Path(failed_path)
        if not failed_path_obj.exists():
            QMessageBox.warning(self, "⚠️ Failed Folder Not Found", 
                               f"Failed folder doesn't exist:\n{failed_path}")
            return
        
        # Count files in failed folder
        failed_files = list(failed_path_obj.rglob("*"))
        media_files = [f for f in failed_files if f.is_file() and is_media_file(f)]
        
        if not media_files:
            QMessageBox.information(self, "ℹ️ No Failed Files", 
                                   "The failed folder is empty or contains no media files to retry.")
            return
        
        # Confirm retry action
        reply = QMessageBox.question(self, "🔄 Retry Failed Files", 
                                   f"Found {len(media_files)} media files in the failed folder.\n\n"
                                   f"This will:\n"
                                   f"• Set source to: {failed_path}\n"
                                   f"• Use current destination settings\n"
                                   f"• Process only the previously failed files\n\n"
                                   f"Continue?",
                                   QMessageBox.Yes | QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            # Set the source to the failed folder
            self.source_dropzone.set_path(failed_path)
            self.append_log(f"🔄 Retry mode: Source set to failed folder with {len(media_files)} files")
            
            # Disable retry button to prevent multiple clicks
            self.btn_retry_failed.setEnabled(False)

    # ---- Slots ----
    def on_progress(self, processed, total):
        pct = int((processed/total)*100) if total else 0
        self.prog.setValue(pct)
        
        # Extract rate information from substage text
        rate = 0
        avg_rate = 0
        stage_elapsed = None
        
        substage_text = self.lbl_substage.text()
        if "Current:" in substage_text and "files/s" in substage_text:
            try:
                # Extract current rate from text like "Current: 2.5 files/s"
                parts = substage_text.split("Current:")
                if len(parts) > 1:
                    rate_part = parts[1].split("files/s")[0].strip()
                    rate = float(rate_part)
                
                # Extract average rate from text like "Avg: 3.1 files/s"
                if "Avg:" in substage_text:
                    avg_parts = substage_text.split("Avg:")
                    if len(avg_parts) > 1:
                        avg_rate_part = avg_parts[1].split("files/s")[0].strip()
                        avg_rate = float(avg_rate_part)
                
                # Extract stage elapsed time from text like "Stage: 02:45"
                if "Stage:" in substage_text:
                    time_parts = substage_text.split("Stage:")
                    if len(time_parts) > 1:
                        time_str = time_parts[1].strip().split()[0]  # Get "02:45" part
                        if ":" in time_str:
                            mins, secs = map(int, time_str.split(":"))
                            stage_elapsed = mins * 60 + secs
            except:
                pass
        
        self.circular_progress.set_progress(pct, processed, total, rate, avg_rate, stage_elapsed)

    def on_thumb(self, path, caption): 
        self.set_thumb(path, caption)

    def on_plan_complete_show_dialog(self, summary: dict):
        size_gb = summary.get("total_bytes",0)/(1024**3)
        dlg = PlanSummaryDialog(self, summary.get("with_json",0), summary.get("without_json",0),
                                summary.get("images",0), summary.get("videos",0),
                                summary.get("live_pairs",0), size_gb)
        
        # Update progress to show planning complete
        self.circular_progress.set_progress(100, summary.get("with_json",0) + summary.get("without_json",0), summary.get("with_json",0) + summary.get("without_json",0), 0)
        self.lbl_stage.setText("✅ Planning Complete")
        
        if dlg.exec() != QDialog.Accepted:
            self.append_log("[UI] User cancelled. No files were modified.")
            self.worker.request_stop()
            self.worker.toggle_pause(False)  # release to exit cleanly
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.btn_start.setEnabled(True)
            self.lbl_stage.setText("🔄 Ready to Start")
            self.circular_progress.set_progress(0, 0, 0, 0)
        else:
            self.append_log("[UI] Starting merge stage...")
            self.worker.toggle_pause(False)
            self.lbl_stage.setText("⚙️ Merging Files")
            self.circular_progress.set_progress(0, 0, 0, 0)

    def on_finished(self, report_csv, log_path):
        self.append_log("✨ === Processing Complete! ===")
        self.append_log(f"📊 Report: {report_csv}")
        self.append_log(f"📝 Log file: {log_path}")
        
        # Update UI to finished state
        # Get current counts from the stats cards
        completed_count = int(self.stats_completed.value_label.text())
        failed_count = int(self.stats_failed.value_label.text())
        total_processed = completed_count + failed_count
        self.circular_progress.set_progress(100, total_processed, total_processed, 0)
        self.lbl_stage.setText("✨ All Done!")
        self.lbl_substage.setText("Processing completed successfully")
        
        # Reset buttons
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self._update_pause_button_style(enabled=False)
        self.btn_pause.setText("⏸️ Pause")
        self._update_pause_button_style(enabled=False)
        
        # Enable retry failed button if there are failed files
        self._check_enable_retry_button()

    def _check_enable_retry_button(self):
        """Enable retry button if failed folder exists and has media files"""
        try:
            failed_path = self.inp_failed.text().strip()
            if not failed_path:
                self.btn_retry_failed.setEnabled(False)
                return
            
            failed_path_obj = Path(failed_path)
            if not failed_path_obj.exists():
                self.btn_retry_failed.setEnabled(False)
                return
            
            # Check for media files in failed folder
            media_files = [f for f in failed_path_obj.rglob("*") if f.is_file() and is_media_file(f)]
            has_failed_files = len(media_files) > 0
            
            self.btn_retry_failed.setEnabled(has_failed_files)
            
            if has_failed_files:
                self.btn_retry_failed.setText(f"🔄 Retry {len(media_files)} Failed Files")
                self.append_log(f"💡 Tip: {len(media_files)} failed files available for retry")
            else:
                self.btn_retry_failed.setText("🔄 Retry Only Failed")
                
        except Exception as e:
            self.btn_retry_failed.setEnabled(False)
            self.append_log(f"Warning: Could not check failed files: {e}")

    def on_failure_summary(self, failure_reasons: dict):
        """Show failure summary dialog if there are failures"""
        total_failures = sum(failure_reasons.values())
        if total_failures > 0:
            self._show_failure_summary_dialog(failure_reasons)

    def _show_failure_summary_dialog(self, failure_reasons: dict):
        """Display detailed failure summary in a dialog"""
        dialog = QDialog(self)
        dialog.setWindowTitle("📊 Failed Files Summary")
        dialog.setMinimumSize(400, 300)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #1a1a1a;
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
            }
            QTableWidget {
                background-color: #2d2d2d;
                border: 1px solid #3d3d3d;
                border-radius: 8px;
                gridline-color: #3d3d3d;
            }
            QTableWidget::item {
                padding: 8px;
                border-bottom: 1px solid #3d3d3d;
            }
            QHeaderView::section {
                background-color: #3d3d3d;
                color: #ffffff;
                padding: 8px;
                border: none;
                font-weight: 600;
            }
        """)
        
        layout = QVBoxLayout(dialog)
        
        # Title
        title = QLabel("❌ Processing Failures Breakdown")
        title.setStyleSheet("""
            font-size: 16px;
            font-weight: 600;
            color: #ff6b6b;
            margin-bottom: 10px;
        """)
        layout.addWidget(title)
        
        # Create table
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        table = QTableWidget()
        table.setRowCount(len([k for k, v in failure_reasons.items() if v > 0]))
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Failure Type", "Count", "Description"])
        
        # Populate table
        row = 0
        reason_descriptions = {
            "no_json": "No JSON metadata file found",
            "bad_json": "JSON file corrupted or invalid",
            "exiftool_error": "ExifTool processing failed",
            "partner_error": "Live Photo partner file failed",
            "other_error": "Unknown or miscellaneous error"
        }
        
        reason_icons = {
            "no_json": "📄",
            "bad_json": "💥",
            "exiftool_error": "🔧",
            "partner_error": "📷",
            "other_error": "❓"
        }
        
        for reason_type, count in failure_reasons.items():
            if count > 0:
                icon = reason_icons.get(reason_type, "❓")
                type_item = QTableWidgetItem(f"{icon} {reason_type.replace('_', ' ').title()}")
                count_item = QTableWidgetItem(str(count))
                desc_item = QTableWidgetItem(reason_descriptions.get(reason_type, "Unknown error"))
                
                # Center align count
                count_item.setTextAlignment(Qt.AlignCenter)
                
                # Color code the count based on severity
                if count > 10:
                    count_item.setForeground(QColor("#ff6b6b"))  # Red for high count
                elif count > 5:
                    count_item.setForeground(QColor("#ffa500"))  # Orange for medium count
                else:
                    count_item.setForeground(QColor("#ffffff"))  # White for low count
                
                table.setItem(row, 0, type_item)
                table.setItem(row, 1, count_item)
                table.setItem(row, 2, desc_item)
                row += 1
        
        # Resize columns to content
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        
        layout.addWidget(table)
        
        # Total summary
        total_failures = sum(failure_reasons.values())
        summary = QLabel(f"Total failed files: {total_failures}")
        summary.setStyleSheet("""
            font-size: 14px;
            font-weight: 600;
            color: #ff6b6b;
            margin-top: 10px;
            background-color: rgba(255, 107, 107, 0.1);
            border-radius: 4px;
            padding: 8px;
        """)
        summary.setAlignment(Qt.AlignCenter)
        layout.addWidget(summary)
        
        # Tip
        tip = QLabel("💡 Use 'Retry Only Failed' button to reprocess failed files")
        tip.setStyleSheet("""
            font-size: 12px;
            color: #aaaaaa;
            margin-top: 10px;
            font-style: italic;
        """)
        tip.setAlignment(Qt.AlignCenter)
        layout.addWidget(tip)
        
        # Close button
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        close_btn = ModernButton("Close", "primary")
        close_btn.clicked.connect(dialog.accept)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)
        
        dialog.exec()

    def on_fatal(self, msg):
        self.append_log(f"❌ FATAL ERROR: {msg}")
        QMessageBox.critical(self, "❌ Fatal Error", msg)
        
        # Reset UI to error state
        self.circular_progress.set_progress(0, 0, 0, 0)
        self.lbl_stage.setText("❌ Error Occurred")
        self.lbl_substage.setText("Check log for details")
        
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self._update_pause_button_style(enabled=False)
    
    def toggle_detailed_log(self):
        """Toggle the detailed log visibility"""
        if self.log.isVisible():
            self.log.hide()
            self.log_toggle_btn.setText("Show Details")
        else:
            self.log.show()
            self.log_toggle_btn.setText("Hide Details")
    
    def _update_pause_button_style(self, enabled: bool):
        """Update pause button visual style based on enabled state"""
        if enabled:
            self.btn_pause.setStyleSheet("""
                ModernButton {
                    background: transparent;
                    color: #ffffff;
                    border: 2px solid #4CAF50;
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
        else:
            # Disabled/grayed out style
            self.btn_pause.setStyleSheet("""
                ModernButton {
                    background: transparent;
                    color: #555555;
                    border: 2px solid #444444;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 500;
                    padding: 10px 20px;
                }
                ModernButton:disabled {
                    background: transparent;
                    color: #555555;
                    border: 2px solid #444444;
                }
            """)

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
            border: 1px solid #4CAF50;
            border-radius: 6px;
            padding: 8px;
            font-size: 12px;
        }
        
        QMessageBox {
            background-color: #1a1a1a;
            color: #ffffff;
        }
        
        QMessageBox QPushButton {
            background-color: #4CAF50;
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
