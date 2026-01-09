"""
AutoKaraoke v0.1
功能：Whisper 自动歌词生成、双语对齐、自定义 Prompt、歌词打轴
"""
import sys
import os
import re
import traceback
import gc
import time
import torch
import stable_whisper
from multiprocessing import Process, Queue, Event
from queue import Empty
from PyQt6.QtWidgets import  QDoubleSpinBox # 记得添加这个

# 镜像源配置
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

try:
    from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                 QHBoxLayout, QPushButton, QLabel, QFileDialog,
                                 QTextEdit, QProgressBar, QMessageBox, QComboBox,
                                 QSplitter, QSpinBox, QDialog, QTableWidget, 
                                 QTableWidgetItem, QHeaderView, QAbstractItemView,
                                 QSlider, QStyle, QLineEdit)
    from PyQt6.QtCore import Qt, QTimer, QUrl
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
except ImportError:
    print("错误: 缺少 PyQt6 库。请运行: pip install PyQt6")
    sys.exit(1)

try:
    import faster_whisper
    HAS_FASTER_WHISPER = True
except ImportError:
    HAS_FASTER_WHISPER = False

# ================= 常量配置 =================
MIN_DURATION = 0.06
SEARCH_WINDOW = 8
TIMEOUT_CHECK_INTERVAL = 0.5

# ================= 歌词解析类 =================
class LrcParser:
    def __init__(self):
        self.headers = []
        self.lines_text = []
        self.translations = {}
        # 匹配制作人信息等非歌词行
        self.credits_pattern = re.compile(
            r"^(作|编|词|曲|演|唱|混|录|母|制|监|统|出|绘|调|和|吉|贝|鼓|弦|管|Lyr|Com|Arr|Sin|Voc|Mix|Mas|Pro|Art|Cov|Gui|Bas|Dru|Str)"
            r".{0,40}"
            r"([:：]|\s|-)", re.IGNORECASE
        )
        self.time_tag_pattern = re.compile(r'^\[\d{2}:\d{2}')
        self.tag_content_pattern = re.compile(r'^(\[\d{2}:\d{2}.*?\])(.*)')
        self.remove_tags_pattern = re.compile(r'\[.*?\]')
        self.remove_html_pattern = re.compile(r'<.*?>')

    def parse(self, content: str, ext: str) -> str:
        self.headers = []
        self.lines_text = []
        self.translations = {}
        
        content = content.lstrip('\ufeff')
        lines = content.splitlines()
        
        last_time_tag = None
        current_index = -1
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            if line.startswith('[') and not self.time_tag_pattern.match(line):
                self.headers.append(line)
                continue
            
            match = self.tag_content_pattern.match(line)
            text_only = ""
            time_tag = ""
            
            if match:
                time_tag = match.group(1)
                text_content = match.group(2).strip()
                text_only = self.remove_tags_pattern.sub('', text_content)
                text_only = self.remove_html_pattern.sub('', text_only).strip()
            else:
                text_only = self.remove_tags_pattern.sub('', line).strip()
            
            if not text_only: continue
            
            if self.credits_pattern.match(text_only):
                self.headers.append(line)
                continue
            
            if time_tag and time_tag == last_time_tag and current_index >= 0:
                if current_index not in self.translations:
                    self.translations[current_index] = []
                self.translations[current_index].append(text_only)
            else:
                self.lines_text.append(text_only)
                current_index += 1
                last_time_tag = time_tag
        
        return "\n".join(self.lines_text)

class WordLevelEditor(QDialog):
    """
    字级精细校对窗口 (支持区间播放与自动暂停)
    """
    # 1. 修改初始化函数，增加 end_time_ms 参数
    def __init__(self, audio_path, line_text, start_time_ms, end_time_ms, parent=None):
        super().__init__(parent)
        self.setWindowTitle("逐字精细打轴 (Enter: 打点 | Space: 播放 | ←/→: 移动)")
        self.resize(1000, 450)
        self.audio_path = audio_path
        self.line_text = line_text
        self.base_time = start_time_ms
        self.end_time_ms = end_time_ms  # 记录本句结束时间
        self.result_text = None
        
        self.tokens = self.parse_line(line_text, start_time_ms)
        self.last_active_idx = -1
        
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.setSource(QUrl.fromLocalFile(audio_path))
        
        # 初始定位到该句开始前 1秒 (稍微留点预卷时间)
        self.start_pos = max(0, self.tokens[0]['time'] - 1000 if self.tokens else start_time_ms - 1000)
        
        self.setup_ui()
        
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.sync_highlight)
        self.timer.start()

    def on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia or status == QMediaPlayer.MediaStatus.BufferedMedia:
            self.player.setPosition(self.start_pos)

    def parse_line(self, text, default_start):
        clean_text = re.sub(r'^\[\d{2}:\d{2}\.\d{2,3}\]', '', text)
        parts = re.split(r'(\[\d{2}:\d{2}\.\d{2,3}\])', clean_text)
        tokens = []
        current_time = default_start
        for part in parts:
            if not part: continue
            if re.match(r'^\[\d{2}:\d{2}\.\d{2,3}\]$', part):
                current_time = self.parse_time_tag(part)
            else:
                chars = list(part)
                for char in chars:
                    tokens.append({'char': char, 'time': current_time, 'edited': False})
        return tokens

    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # 顶部信息栏
        info_lay = QHBoxLayout()
        # 显示当前校对的区间
        range_str = f"当前区间: {self.format_ms(self.base_time)} -> {self.format_ms(self.end_time_ms)}"
        info_lay.addWidget(QLabel(f"<b>{range_str}</b>"))
        layout.addLayout(info_lay)

        # 控制栏
        top_lay = QHBoxLayout()
        self.btn_play = QPushButton("播放/暂停 (Space)")
        self.btn_play.clicked.connect(self.toggle_play)
        
        self.lbl_speed = QLabel("倍速:")
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.25, 2.0)
        self.spin_speed.setSingleStep(0.1)
        self.spin_speed.setValue(1.0)
        self.spin_speed.setSuffix(" x")
        self.spin_speed.valueChanged.connect(self.change_speed)
        
        self.lbl_time = QLabel("00:00.000")
        self.lbl_time.setStyleSheet("font-size: 16px; font-weight: bold; color: #409eff;")
        
        top_lay.addWidget(self.btn_play)
        top_lay.addWidget(self.lbl_speed)
        top_lay.addWidget(self.spin_speed)
        top_lay.addStretch()
        top_lay.addWidget(self.lbl_time)
        layout.addLayout(top_lay)
        
        # 表格控件
        self.table = QTableWidget()
        self.table.setRowCount(2)
        self.table.setVerticalHeaderLabels(["歌词", "时间"])
        self.table.setColumnCount(len(self.tokens))
        self.table.horizontalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectColumns)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        
        for col, token in enumerate(self.tokens):
            item_char = QTableWidgetItem(token['char'])
            item_char.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            font = item_char.font()
            font.setPointSize(20)
            item_char.setFont(font)
            self.table.setItem(0, col, item_char)
            
            time_str = self.format_ms(token['time'])
            item_time = QTableWidgetItem(time_str)
            item_time.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(1, col, item_time)
            
        self.table.resizeRowsToContents()
        for i in range(self.table.columnCount()):
            self.table.setColumnWidth(i, 60)
            
        self.table.cellClicked.connect(self.on_cell_clicked)
        layout.addWidget(self.table)
        
        # 底部按钮
        btn_box = QHBoxLayout()
        btn_replay = QPushButton("⏪ 重播本句")
        btn_replay.clicked.connect(lambda: self.player.setPosition(self.start_pos))
        
        btn_save = QPushButton("💾 确认并保存")
        btn_save.setStyleSheet("background: #67c23a; color: white; font-weight: bold; padding: 10px;")
        btn_save.clicked.connect(self.save_and_close)
        
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        
        btn_box.addWidget(btn_replay)
        btn_box.addStretch()
        btn_box.addWidget(btn_save)
        btn_box.addWidget(btn_cancel)
        layout.addLayout(btn_box)
        
        if self.table.columnCount() > 0:
            self.table.selectColumn(0)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            # 如果当前已经播放到了结束时间后面，重新从头播放
            if self.player.position() >= self.end_time_ms:
                self.player.setPosition(self.start_pos)
            self.player.play()

    def change_speed(self, val):
        self.player.setPlaybackRate(val)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_play()
        elif event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            self.stamp_current_char()
        elif event.key() == Qt.Key.Key_Left:
            curr = self.table.currentColumn()
            if curr > 0: self.table.selectColumn(curr - 1)
        elif event.key() == Qt.Key.Key_Right:
            curr = self.table.currentColumn()
            if curr < self.table.columnCount() - 1: self.table.selectColumn(curr + 1)
        else:
            super().keyPressEvent(event)

    def stamp_current_char(self):
        curr_col = self.table.currentColumn()
        if curr_col < 0: return
        
        current_pos = self.player.position()
        self.tokens[curr_col]['time'] = current_pos
        self.tokens[curr_col]['edited'] = True
        
        self.table.item(1, curr_col).setText(self.format_ms(current_pos))
        self.update_cell_color(curr_col, is_active=True)
        
        if curr_col < self.table.columnCount() - 1:
            self.table.selectColumn(curr_col + 1)

    def sync_highlight(self):
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
            
        pos = self.player.position()
        self.lbl_time.setText(self.format_ms(pos))
        
        # === 核心逻辑：超过本句结束时间自动暂停 ===
        # 允许超过 200ms 的缓冲，避免听到下一句的头
        if pos >= self.end_time_ms + 200:
            self.player.pause()
            self.update_play_icon()
            return
        # ======================================
        
        active_idx = -1
        for i, token in enumerate(self.tokens):
            if pos >= token['time']:
                active_idx = i
            else:
                break
        
        if active_idx != self.last_active_idx:
            if self.last_active_idx >= 0 and self.last_active_idx < self.table.columnCount():
                self.update_cell_color(self.last_active_idx, is_active=False)
            
            if active_idx >= 0 and active_idx < self.table.columnCount():
                self.update_cell_color(active_idx, is_active=True)
                self.table.scrollToItem(self.table.item(0, active_idx))
            
            self.last_active_idx = active_idx

    def update_cell_color(self, col, is_active):
        token = self.tokens[col]
        item = self.table.item(0, col)
        if not item: return

        if is_active: bg = Qt.GlobalColor.cyan
        elif token['edited']: bg = Qt.GlobalColor.yellow
        else: bg = Qt.GlobalColor.white
            
        if item.background().color() != bg:
            item.setBackground(bg)

    def update_play_icon(self):
        # 简单的图标更新
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play.setText("暂停 (Space)")
        else:
            self.btn_play.setText("播放 (Space)")

    def on_cell_clicked(self, row, col):
        time_ms = self.tokens[col]['time']
        self.player.setPosition(time_ms)
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def save_and_close(self):
        content_str = ""
        first_time_str = f"[{self.format_ms(self.tokens[0]['time'])}]"
        for i, token in enumerate(self.tokens):
            t_str = f"[{self.format_ms(token['time'])}]"
            if i == 0: content_str += token['char']
            else: content_str += f"{t_str}{token['char']}"
        
        self.result_lrc_content = content_str
        self.result_start_time = first_time_str
        self.player.stop()
        self.accept()

    def format_ms(self, ms):
        seconds = ms / 1000
        return f"{int(seconds//60):02d}:{int(seconds%60):02d}.{int((seconds%1)*1000):03d}"

    def parse_time_tag(self, tag):
        try:
            parts = tag.strip("[]").split(':')
            return int((int(parts[0]) * 60 + float(parts[1])) * 1000)
        except: return 0

# ================= 歌词编辑器窗口 =================
class LrcEditorDialog(QDialog):
    def __init__(self, audio_path, lrc_content, parent=None):
        super().__init__(parent)
        self.setWindowTitle("歌词精细校准 - AutoKaraoke Editor")
        self.resize(1000, 750) 
        self.audio_path = audio_path
        self.lrc_content = lrc_content
        self.result_lrc = None
        
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        
        self.setup_ui()
        self.load_lrc_data()
        self.load_audio()
        
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_progress)
        self.timer.start()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        help_lbl = QLabel(
            "💡 <b>操作：</b>单击暂停选中 | 双击跳转 | Enter键同步当前行 | 空格播放/暂停"
        )
        help_lbl.setStyleSheet("background: #e6f7ff; padding: 10px; border: 1px solid #91d5ff;")
        layout.addWidget(help_lbl)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["时间戳", "歌词内容"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        
        self.table.cellDoubleClicked.connect(self.seek_to_row)
        self.table.cellPressed.connect(self.pause_on_click)
        layout.addWidget(self.table)
        
        ctrl_box = QHBoxLayout()
        self.btn_play = QPushButton()
        self.update_play_icon()
        self.btn_play.clicked.connect(self.toggle_play)
        
        self.lbl_curr = QLabel("00:00.000")
        self.lbl_curr.setMinimumWidth(80)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self.set_position)
        self.slider.sliderPressed.connect(self.pause_for_seek)
        self.slider.sliderReleased.connect(self.resume_after_seek)
        self.lbl_total = QLabel("00:00.000")
        
        ctrl_box.addWidget(self.btn_play)
        ctrl_box.addWidget(self.lbl_curr)
        ctrl_box.addWidget(self.slider)
        ctrl_box.addWidget(self.lbl_total)
        layout.addLayout(ctrl_box)
        
        btn_box = QHBoxLayout()
        btn_stamp = QPushButton("⏱️ 智能同步写入 (Enter)")
        btn_stamp.setStyleSheet("background: #e6a23c; color: white; font-weight: bold;")
        btn_stamp.clicked.connect(self.stamp_current_time)
        
        btn_save = QPushButton("💾 保存并关闭")
        btn_save.setStyleSheet("background: #67c23a; color: white; font-weight: bold;")
        btn_save.clicked.connect(self.save_lrc) 
        
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject) 
        
        btn_box.addWidget(btn_stamp)
        btn_box.addStretch()
        btn_box.addWidget(btn_save)
        btn_box.addWidget(btn_cancel)
        layout.addLayout(btn_box)
        
        self.table.keyPressEvent = self.table_key_event

    def load_audio(self):
        if self.audio_path and os.path.exists(self.audio_path):
            self.player.setSource(QUrl.fromLocalFile(self.audio_path))
            self.player.mediaStatusChanged.connect(self.on_media_status)
    
    def on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            duration = self.player.duration()
            self.slider.setRange(0, duration)
            self.lbl_total.setText(self.format_ms(duration))

    def load_lrc_data(self):
        lines = self.lrc_content.splitlines()
        self.table.setRowCount(0)
        pattern = re.compile(r'^(\[\d{2}:\d{2}\.\d{2,3}\])(.*)')
        row = 0
        for line in lines:
            line = line.strip()
            if not line: continue
            match = pattern.match(line)
            if match:
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(match.group(1)))
                self.table.setItem(row, 1, QTableWidgetItem(match.group(2)))
                row += 1
            else:
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(""))
                self.table.setItem(row, 1, QTableWidgetItem(line))
                row += 1

    def table_key_event(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_play()
        elif event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            self.stamp_current_time()
        else:
            QTableWidget.keyPressEvent(self.table, event)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()
        self.update_play_icon()

    def update_play_icon(self):
        style = self.style()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        else:
            icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.btn_play.setIcon(icon)

    def update_progress(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            pos = self.player.position()
            self.slider.setValue(pos)
            self.lbl_curr.setText(self.format_ms(pos))

    def set_position(self, pos):
        self.player.setPosition(pos)
        self.lbl_curr.setText(self.format_ms(pos))

    def pause_for_seek(self):
        self.was_playing = (self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        self.player.pause()

    def resume_after_seek(self):
        if hasattr(self, 'was_playing') and self.was_playing:
            self.player.play()
            self.update_play_icon()
    
    def seek_to_row(self, row, col):
        """
        双击进入逐字编辑模式
        """
        # 获取当前行的时间和文本
        time_item = self.table.item(row, 0)
        text_item = self.table.item(row, 1)
        
        if not time_item or not text_item: return
        
        time_str = time_item.text()
        text_content = text_item.text()
        start_ms = self.parse_time_tag(time_str)
        
        # === 关键：计算本句的结束时间 (下一句的开始时间) ===
        end_ms = self.player.duration() # 默认为歌曲总时长
        next_row = row + 1
        
        # 寻找下一个有效的时间戳作为结束时间
        while next_row < self.table.rowCount():
            next_time_item = self.table.item(next_row, 0)
            if next_time_item:
                next_start_ms = self.parse_time_tag(next_time_item.text())
                if next_start_ms > start_ms: # 确保下一句时间确实比这句晚
                    end_ms = next_start_ms
                    break
            next_row += 1
        # =================================================
        
        # 暂停主播放器
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.update_play_icon()
            
        # 打开逐字编辑器，传入结束时间
        editor = WordLevelEditor(self.audio_path, text_content, start_ms, end_ms, self)
        
        if editor.exec():
            # 保存逻辑 (保持不变)
            if hasattr(editor, 'result_start_time'):
                self.table.setItem(row, 0, QTableWidgetItem(editor.result_start_time))
            if hasattr(editor, 'result_lrc_content'):
                self.table.setItem(row, 1, QTableWidgetItem(editor.result_lrc_content))
    
    def pause_on_click(self, row, col):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.update_play_icon()

    def stamp_current_time(self):
        current_rows = self.table.selectedItems()
        if not current_rows: return
        
        row = current_rows[0].row()
        current_pos_ms = self.player.position()
        new_time_str = f"[{self.format_ms(current_pos_ms)}]"
        
        old_time_item = self.table.item(row, 0)
        old_time_str = old_time_item.text()
        old_start_ms = self.parse_time_tag(old_time_str)
        
        lyric_item = self.table.item(row, 1)
        original_text = lyric_item.text()
        
        delta_ms = 0
        if old_start_ms >= 0:
            delta_ms = current_pos_ms - old_start_ms

        # 修复首字异常空隙
        extra_fix_ms = 0
        first_inner_match = re.search(r'\[(\d{2}:\d{2}\.\d{2,3})\]', original_text)
        if first_inner_match and old_start_ms >= 0:
            old_first_inner_ms = self.parse_time_tag(f"[{first_inner_match.group(1)}]")
            original_gap = old_first_inner_ms - old_start_ms
            if original_gap > 1200:
                target_gap = 300 
                extra_fix_ms = -(original_gap - target_gap)

        self.table.setItem(row, 0, QTableWidgetItem(new_time_str))
        
        total_shift_ms = delta_ms + extra_fix_ms
        shifted_text = self.shift_timestamps_in_string(original_text, total_shift_ms)
        self.table.setItem(row, 1, QTableWidgetItem(shifted_text))
        
        # 同步更新后续翻译行
        next_row = row + 1
        while next_row < self.table.rowCount():
            next_time_item = self.table.item(next_row, 0)
            if not next_time_item: break
            if next_time_item.text() == old_time_str:
                self.table.setItem(next_row, 0, QTableWidgetItem(new_time_str))
                next_row += 1
            else:
                break
        
        if row < self.table.rowCount() - 1:
            self.table.selectRow(row + 1)
            self.table.scrollToItem(self.table.item(row + 1, 0))

    def shift_timestamps_in_string(self, text, delta_ms):
        def replace_func(match):
            full_tag = match.group(0)
            ms = self.parse_time_tag(full_tag)
            if ms < 0: return full_tag
            new_ms = max(0, ms + delta_ms)
            return f"[{self.format_ms(new_ms)}]"
        
        pattern = re.compile(r'\[\d{2}:\d{2}\.\d{2,3}\]')
        return pattern.sub(replace_func, text)

    def save_lrc(self):
        lines = []
        for r in range(self.table.rowCount()):
            t = self.table.item(r, 0).text()
            c = self.table.item(r, 1).text()
            lines.append(f"{t}{c}")
        self.result_lrc = "\n".join(lines)
        self.accept()
    
    def format_ms(self, ms):
        seconds = ms / 1000
        m = int(seconds // 60)
        s = int(seconds % 60)
        rem = int((seconds % 1) * 1000)
        return f"{m:02d}:{s:02d}.{rem:03d}"
        
    def parse_time_tag(self, tag):
        try:
            clean = tag.strip("[]")
            parts = clean.split(':')
            return int((int(parts[0]) * 60 + float(parts[1])) * 1000)
        except:
            return -1

    def stop_and_release(self):
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.player.stop()

    def accept(self):
        self.stop_and_release()
        super().accept()
        
    def reject(self):
        self.stop_and_release()
        super().reject()

    def closeEvent(self, event):
        self.stop_and_release()
        event.accept()

# ================= 后台处理进程 =================
def worker_process(audio_path, model_size, language, ref_text,
                   lrc_parser_data, time_offset, initial_prompt_input, 
                   result_queue, progress_queue, stop_event):
    try:
        parser = LrcParser()
        parser.headers = lrc_parser_data.get('headers', [])
        parser.lines_text = lrc_parser_data.get('lines_text', [])
        parser.translations = lrc_parser_data.get('translations', {})
        
        def get_attr(obj, key, default=None):
            if isinstance(obj, dict): return obj.get(key, default)
            return getattr(obj, key, default)
        
        def format_time(seconds):
            final_sec = max(0, float(seconds) + time_offset)
            m = int(final_sec // 60)
            s = int(final_sec % 60)
            ms = int((final_sec % 1) * 1000)
            return f"{m:02d}:{s:02d}.{ms:03d}"
        
        def clean_token(text):
            return re.sub(r'[^\w\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff]', '', text).lower()
        
        def preprocess_cjk_spaces(text):
            if not text: return text
            pattern = r'([\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff])'
            spaced = re.sub(pattern, r' \1 ', text)
            return re.sub(r'\s+', ' ', spaced).strip()
        
        def reconstruct_lrc_smart(result):
            output_lines = []
            for h in parser.headers: output_lines.append(h)
            if parser.headers: output_lines.append("")
            
            # 无参考文本：直接转录
            if not parser.lines_text:
                segments = get_attr(result, 'segments', [])
                if not segments:
                    try: segments = list(result)
                    except: pass
                for seg in segments:
                    if stop_event.is_set(): return ""
                    start = get_attr(seg, 'start', 0)
                    text = get_attr(seg, 'text', '').strip()
                    if text: output_lines.append(f"[{format_time(start)}]{text}")
                return "\n".join(output_lines)
            
            # 有参考文本：双语对齐逻辑
            progress_queue.put("正在执行双语防撞对齐...")
            ai_words_pool = []
            segments = get_attr(result, 'segments', [])
            if not segments:
                try: segments = list(result)
                except: pass
            
            for seg in segments:
                words = get_attr(seg, 'words', [])
                if words: ai_words_pool.extend(words)
            
            pool_cursor = 0
            total_ai_words = len(ai_words_pool)
            last_valid_time = 0.0
            
            for i, target_line in enumerate(parser.lines_text):
                if stop_event.is_set(): return ""
                
                line_tokens = []
                token_iter = re.finditer(r'([a-zA-Z0-9\']+|[\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff])', target_line)
                last_end_idx = 0
                
                for match in token_iter:
                    pre_text = target_line[last_end_idx:match.start()].replace("\n", "")
                    token_text = match.group()
                    last_end_idx = match.end()
                    
                    matched_time = None
                    user_clean = clean_token(token_text)
                    
                    for offset in range(SEARCH_WINDOW):
                        if pool_cursor + offset >= total_ai_words: break
                        ai_w_obj = ai_words_pool[pool_cursor + offset]
                        ai_text = get_attr(ai_w_obj, 'word', "")
                        ai_clean = clean_token(ai_text)
                        
                        if user_clean and ai_clean and (user_clean in ai_clean or ai_clean in user_clean):
                            w_start = get_attr(ai_w_obj, 'start', 0.0)
                            if w_start >= last_valid_time:
                                matched_time = w_start
                                pool_cursor = pool_cursor + offset + 1
                            break
                    
                    line_tokens.append({"text": token_text, "pre": pre_text, "time": matched_time})
                
                count = len(line_tokens)
                if count == 0:
                    output_lines.append(target_line)
                    continue
                
                # 插值补全
                for k in range(count):
                    if line_tokens[k]["time"] is None:
                        prev_time = last_valid_time
                        for j in range(k - 1, -1, -1):
                            if line_tokens[j]["time"] is not None:
                                prev_time = line_tokens[j]["time"]
                                break
                        next_time = None
                        steps = 1
                        for j in range(k + 1, count):
                            steps += 1
                            if line_tokens[j]["time"] is not None:
                                next_time = line_tokens[j]["time"]
                                break
                        
                        if next_time is not None:
                            gap = (next_time - prev_time) / steps
                            gap = max(MIN_DURATION, min(gap, 0.15))
                            line_tokens[k]["time"] = prev_time + gap
                        else:
                            line_tokens[k]["time"] = prev_time + 0.15
                
                line_str = ""
                effective_start_time = None
                
                for k, item in enumerate(line_tokens):
                    t = item["time"]
                    if t < last_valid_time + MIN_DURATION: t = last_valid_time + MIN_DURATION
                    last_valid_time = t
                    if k == 0: effective_start_time = t
                    
                    if k == 0 and item["pre"].strip():
                        line_str += f"[{format_time(t)}]{item['pre']}{item['text']}"
                    else:
                        line_str += f"{item['pre']}[{format_time(t)}]{item['text']}"
                
                line_str += target_line[last_end_idx:]
                output_lines.append(line_str)
                
                # 挂载翻译
                if i in parser.translations:
                    final_time = effective_start_time if effective_start_time is not None else last_valid_time
                    for trans_text in parser.translations[i]:
                        output_lines.append(f"[{format_time(final_time)}]{trans_text}")
            
            return "\n".join(output_lines)
        
        def clear_vram(model):
            try:
                if model:
                    if hasattr(model, 'to'): model.to("cpu")
                    del model
            except: pass
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        
        # --- 进程主逻辑 ---
        current_dir = os.getcwd()
        local_model_path = os.path.join(current_dir, "models")
        os.makedirs(local_model_path, exist_ok=True)
        
        is_cuda = torch.cuda.is_available()
        device = "cuda" if is_cuda else "cpu"
        progress_queue.put(f"⚙️ 运行设备: {device.upper()}")

        model = None
        try:
            use_faster = False
            # 优先加载 Faster-Whisper
            if HAS_FASTER_WHISPER and not stop_event.is_set():
                progress_queue.put(f"🚀 加载 Faster-Whisper ({model_size})...")
                try:
                    model = stable_whisper.load_faster_whisper(
                        model_size, download_root=local_model_path, device=device,
                        compute_type="float16" if device=="cuda" else "int8"
                    )
                    use_faster = True
                except Exception as fw_error:
                    print(f"Faster-Whisper 加载失败: {fw_error}")
                    model = None
            
            # 回退标准模型
            if not model and not stop_event.is_set():
                progress_queue.put(f"加载标准模型 ({model_size})...")
                model = stable_whisper.load_model(model_size, download_root=local_model_path, device=device)
            
            lang_param = language if language != "Auto (混合)" else None
            
            # 自动检测语言
            if ref_text and not lang_param and not stop_event.is_set():
                progress_queue.put("正在检测语言...")
                try:
                    if use_faster:
                        lang_param = "ja" 
                    else:
                        import whisper
                        audio = whisper.load_audio(audio_path)
                        audio = whisper.pad_or_trim(audio)
                        mel = whisper.log_mel_spectrogram(audio).to(model.device)
                        _, probs = model.detect_language(mel)
                        lang_param = max(probs, key=probs.get)
                except:
                    lang_param = "ja"
            
            if lang_param is None and ref_text: lang_param = "ja"
            
            result = None
            if stop_event.is_set():
                result_queue.put(("aborted", None))
                return
            
            if ref_text and ref_text.strip():
                progress_queue.put("正在进行【结构化强制对齐】...")
                spaced_ref_text = preprocess_cjk_spaces(ref_text)
                result = model.align(audio_path, spaced_ref_text, language=lang_param, regroup=False)
            else:
                progress_queue.put("正在进行语音识别...")
                transcribe_args = {"language": lang_param, "word_timestamps": True, "vad": True, "regroup": False}
                if initial_prompt_input and initial_prompt_input.strip():
                    transcribe_args["initial_prompt"] = initial_prompt_input.strip()
                if use_faster:
                    transcribe_args["beam_size"] = 5
                
                result = model.transcribe(audio_path, **transcribe_args)
            
            if stop_event.is_set():
                result_queue.put(("aborted", None))
                return
            
            progress_queue.put("正在合成结果...")
            lrc_content = reconstruct_lrc_smart(result)
            
            if stop_event.is_set():
                result_queue.put(("aborted", None))
            else:
                result_queue.put(("success", lrc_content))
        
        except torch.cuda.OutOfMemoryError:
            result_queue.put(("error", "❌ 显存不足！请尝试更小的模型"))
        except Exception as e:
            if not stop_event.is_set():
                traceback.print_exc()
                result_queue.put(("error", f"错误: {str(e)}"))
        finally:
            clear_vram(model)
            
    except Exception as e:
        result_queue.put(("error", f"进程错误: {str(e)}"))

# ================= 主程序界面 =================
class LyricsGenApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoKaraoke 0.1")
        self.resize(1100, 900)
        self.lrc_parser = LrcParser()
        self.audio_path = None
        self.worker_process = None
        self.result_queue = None
        self.progress_queue = None
        self.stop_event = None
        self.check_timer = None
        self.setup_ui()
    
    def setup_ui(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f5f7fa; }
            QLabel { font-family: 'Microsoft YaHei'; color: #333; font-size: 13px; }
            QTextEdit { background: white; border: 1px solid #dcdfe6; border-radius: 6px; padding: 10px; font-family: Consolas; font-size: 14px; }
            QLineEdit { background: white; border: 1px solid #dcdfe6; border-radius: 4px; padding: 5px; }
            QPushButton { background-color: #409eff; color: white; border-radius: 6px; padding: 8px 15px; font-weight: bold; }
            QPushButton:hover { background-color: #66b1ff; }
            QPushButton:disabled { background-color: #c0c4cc; color: #909399; }
            QComboBox, QSpinBox { padding: 5px; border: 1px solid #dcdfe6; background: white; border-radius: 4px; }
            QProgressBar { border: 1px solid #dcdfe6; border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background-color: #409eff; width: 20px; }
        """)
        
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        
        status_text = "<span style='color:green'>⚡ 加速模式</span>" if HAS_FASTER_WHISPER else "<span>标准模式</span>"
        layout.addWidget(QLabel(f"<h2>AutoKaraoke 0.1 {status_text}</h2>"), alignment=Qt.AlignmentFlag.AlignCenter)
        
        # 文件选择
        file_box = QHBoxLayout()
        self.path_lbl = QLabel("🚫 尚未选择音频文件")
        self.path_lbl.setStyleSheet("background: white; padding: 8px; border: 1px dashed #ccc; border-radius: 4px;")
        btn_aud = QPushButton("📂 选择歌曲")
        btn_aud.clicked.connect(self.select_audio)
        file_box.addWidget(self.path_lbl, 4)
        file_box.addWidget(btn_aud, 1)
        layout.addLayout(file_box)
        
        # 设置区域
        set_box = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.addItems(["tiny", "base", "small", "medium", "large-v2", "large-v3"])
        self.model_combo.setCurrentText("large-v2")
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["Auto (混合)", "zh", "en", "ja", "ko", "yue"])
        self.lang_combo.setCurrentText("ja")
        self.lang_combo.currentTextChanged.connect(self.update_prompt_defaults)
        
        set_box.addWidget(QLabel("AI模型:"))
        set_box.addWidget(self.model_combo)
        set_box.addWidget(QLabel("语言:"))
        set_box.addWidget(self.lang_combo)
        set_box.addWidget(QLabel("⏱️ 整体偏移:"))
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-10000, 10000)
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setValue(0)
        set_box.addWidget(self.offset_spin)
        set_box.addStretch()
        layout.addLayout(set_box)
        
        # Prompt区域
        prompt_box = QHBoxLayout()
        prompt_box.addWidget(QLabel("提示词 (Prompt):"))
        self.prompt_input = QLineEdit()
        self.prompt_input.setPlaceholderText("可引导模型生成风格 (留空则使用默认)")
        self.update_prompt_defaults(self.lang_combo.currentText())
        prompt_box.addWidget(self.prompt_input)
        layout.addLayout(prompt_box)
        
        # 分割区域
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # 左侧输入
        left = QWidget()
        l_lay = QVBoxLayout(left)
        h_lay = QHBoxLayout()
        h_lay.addWidget(QLabel("<b>📝 歌词底稿</b>"))
        btn_imp = QPushButton("📂 导入 LRC/TXT")
        btn_imp.setStyleSheet("background:#e6a23c; color: white;")
        btn_imp.clicked.connect(self.import_lrc)
        btn_clr = QPushButton("🗑️ 清空")
        btn_clr.setStyleSheet("background:#f56c6c; color: white;")
        btn_clr.clicked.connect(lambda: self.input_txt.clear())
        h_lay.addWidget(btn_imp)
        h_lay.addWidget(btn_clr)
        h_lay.addStretch()
        l_lay.addLayout(h_lay)
        self.input_txt = QTextEdit()
        self.input_txt.setPlaceholderText("在此粘贴包含时间戳的LRC...\n第一行为原文，后续相同时间戳的行为翻译。")
        l_lay.addWidget(self.input_txt)
        splitter.addWidget(left)
        
        # 右侧输出
        right = QWidget()
        r_lay = QVBoxLayout(right)
        r_head_lay = QHBoxLayout()
        r_head_lay.addWidget(QLabel("<b>✅ 生成结果</b>"))
        self.btn_cali = QPushButton("🛠️ 手动校准/编辑")
        self.btn_cali.setStyleSheet("background: #909399; color: white;")
        self.btn_cali.clicked.connect(self.open_calibration)
        self.btn_cali.setEnabled(False)
        r_head_lay.addStretch()
        r_head_lay.addWidget(self.btn_cali)
        r_lay.addLayout(r_head_lay)
        self.out_txt = QTextEdit()
        self.out_txt.setStyleSheet("background:#f0f9eb; color: #333;")
        self.out_txt.setReadOnly(True)
        r_lay.addWidget(self.out_txt)
        splitter.addWidget(right)
        layout.addWidget(splitter, 1)
        
        # 底部控制
        btm = QHBoxLayout()
        self.btn_run = QPushButton("🚀 开始生成")
        self.btn_run.clicked.connect(self.start)
        self.btn_run.setMinimumHeight(35)
        self.btn_stop = QPushButton("⏹️ 停止")
        self.btn_stop.setStyleSheet("background:#f56c6c; color: white;")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setMinimumHeight(35)
        btm.addWidget(self.btn_run, 2)
        btm.addWidget(self.btn_stop, 1)
        layout.addLayout(btm)
        
        # 状态栏
        stat = QHBoxLayout()
        self.status = QLabel("就绪")
        self.pbar = QProgressBar()
        self.pbar.setTextVisible(False)
        self.pbar.setMaximumHeight(10)
        self.pbar.hide()
        stat.addWidget(self.status)
        stat.addWidget(self.pbar)
        stat.addStretch()
        stat.addWidget(QLabel("保存编码:"))
        self.enc_combo = QComboBox()
        self.enc_combo.addItems(["utf-8", "gbk", "utf-8-sig"])
        stat.addWidget(self.enc_combo)
        btn_save = QPushButton("💾 保存结果")
        btn_save.clicked.connect(self.save)
        stat.addWidget(btn_save)
        layout.addLayout(stat)

    def update_prompt_defaults(self, lang_text):
        defaults = {
            "zh": "这是一首语速很快的Vocaloid中文歌曲。",
            "ja": "这是一首日语歌曲。",
            "en": "This is a pop song.",
            "yue": "这是一首粤语歌曲。",
            "ko": "This is a Korean song."
        }
        self.prompt_input.setText(defaults.get(lang_text, ""))

    def check_queue(self):
        if self.worker_process and not self.worker_process.is_alive():
            self.on_aborted()
            self.cleanup_worker()
            return
        while True:
            try:
                progress_msg = self.progress_queue.get_nowait()
                self.status.setText(progress_msg)
            except Empty: break
        try:
            result_type, result_data = self.result_queue.get_nowait()
            if result_type == "success": self.on_done(result_data)
            elif result_type == "error": self.on_error(result_data)
            elif result_type == "aborted": self.on_aborted()
            self.cleanup_worker()
        except Empty: pass

    def select_audio(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择音频", "", "Audio Files (*.mp3 *.wav *.flac *.m4a *.ogg)")
        if f:
            self.audio_path = f
            self.path_lbl.setText(f"🎵 {os.path.basename(f)}")
            self.status.setText("音频已加载")
            if self.out_txt.toPlainText().strip(): self.btn_cali.setEnabled(True)

    def import_lrc(self):
        f, _ = QFileDialog.getOpenFileName(self, "导入歌词", "", "Lrc/Txt/Srt (*.lrc *.txt *.srt)")
        if not f: return
        try:
            raw = ""
            for enc in ['utf-8', 'gbk', 'utf-8-sig', 'big5']:
                try:
                    with open(f, 'r', encoding=enc) as file: raw = file.read(); break
                except: continue
            
            ext = os.path.splitext(f)[1].lower()
            clean_text = self.lrc_parser.parse(raw, ext)
            self.input_txt.setText(clean_text)
            self.status.setText(f"导入成功: {os.path.basename(f)}")
        except Exception as e:
            QMessageBox.warning(self, "导入错误", str(e))

    def start(self):
        if not self.audio_path: return QMessageBox.warning(self, "提示", "请先选择音频文件")
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.model_combo.setEnabled(False)
        self.btn_cali.setEnabled(False)
        self.pbar.show()
        self.pbar.setRange(0, 0)
        
        txt = self.input_txt.toPlainText()
        prompt_text = self.prompt_input.text()
        
        lrc_parser_data = {'headers': self.lrc_parser.headers, 'lines_text': self.lrc_parser.lines_text, 'translations': self.lrc_parser.translations}
        
        self.result_queue = Queue()
        self.progress_queue = Queue()
        self.stop_event = Event()
        
        self.worker_process = Process(
            target=worker_process,
            args=(self.audio_path, self.model_combo.currentText(), self.lang_combo.currentText(),
                  txt, lrc_parser_data, self.offset_spin.value()/1000.0,
                  prompt_text,
                  self.result_queue, self.progress_queue, self.stop_event)
        )
        self.worker_process.start()
        self.check_timer = QTimer()
        self.check_timer.timeout.connect(self.check_queue)
        self.check_timer.start(int(TIMEOUT_CHECK_INTERVAL * 1000))

    def stop(self):
        if self.worker_process and self.worker_process.is_alive():
            self.status.setText("正在请求停止...")
            self.stop_event.set()
            self.worker_process.terminate()

    def cleanup_worker(self):
        if self.check_timer: self.check_timer.stop(); self.check_timer = None
        if self.worker_process:
            if self.worker_process.is_alive(): self.worker_process.terminate()
            self.worker_process.join(timeout=1)
            self.worker_process = None
        self.result_queue = None
        self.progress_queue = None
        self.stop_event = None

    def on_done(self, lrc: str):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.model_combo.setEnabled(True)
        self.btn_cali.setEnabled(True)
        self.pbar.hide()
        self.out_txt.setText(lrc)
        self.status.setText("✅ 任务完成")

    def on_aborted(self):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.model_combo.setEnabled(True)
        self.pbar.hide()
        self.status.setText("🛑 任务已停止")

    def on_error(self, error_msg: str):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.model_combo.setEnabled(True)
        self.pbar.hide()
        self.status.setText("❌ 任务失败")
        QMessageBox.critical(self, "错误", error_msg)

    def open_calibration(self):
        if not self.audio_path: return QMessageBox.warning(self, "提示", "没有加载音频文件")
        content = self.out_txt.toPlainText()
        if not content: return QMessageBox.warning(self, "提示", "没有歌词内容")
        
        dialog = LrcEditorDialog(self.audio_path, content, self)
        if dialog.exec():
            if dialog.result_lrc:
                self.out_txt.setText(dialog.result_lrc)
                self.status.setText("✅ 校准已应用")

    def save(self):
        txt = self.out_txt.toPlainText()
        if not txt: return
        default = os.path.splitext(os.path.basename(self.audio_path))[0] + ".lrc" if self.audio_path else "out.lrc"
        f, _ = QFileDialog.getSaveFileName(self, "保存歌词", default, "LRC (*.lrc)")
        if f:
            try:
                with open(f, 'w', encoding=self.enc_combo.currentText()) as file: file.write(txt)
                self.status.setText(f"💾 已保存: {os.path.basename(f)}")
            except Exception as e:
                QMessageBox.critical(self, "保存失败", str(e))

    def closeEvent(self, event):
        if self.worker_process and self.worker_process.is_alive():
            reply = QMessageBox.question(self, '确认退出', '后台任务正在运行，确定要退出吗？', 
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
                                         QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.stop()
                time.sleep(0.5)
                self.cleanup_worker()
                event.accept()
            else: event.ignore()
        else: event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    try: app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    except: pass
    window = LyricsGenApp()
    window.show()
    sys.exit(app.exec())