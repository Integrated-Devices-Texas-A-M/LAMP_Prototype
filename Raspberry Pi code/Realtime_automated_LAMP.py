#!/usr/bin/env python3
"""Real-time image acquisition and colorimetric analysis for a LAMP device.

This application controls two illumination channels and a Raspberry Pi camera,
captures single images or time-lapse image series, segments five reaction tubes,
and calculates the median yellowing index in each tube. It can perform either
post-run folder analysis or analysis immediately after each captured frame.

Yellowing index definition:
    YI = (G - B) / (R + G + B)

Hardware assumptions
--------------------
* Raspberry Pi running Picamera2
* Camera configured for RGB888 acquisition
* Two LED channels connected through GPIO pins 16 and 26
* Five reaction tubes at fixed seed-point coordinates

The analysis algorithm and numerical settings are retained from the working
experimental version; this revision primarily improves readability,
documentation, naming, and maintainability for publication.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime

from gpiozero import LED
from picamera2 import Picamera2
from PyQt5 import QtCore
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# -----------------------------------------------------------------------------
# Application and hardware configuration
# -----------------------------------------------------------------------------
OUTPUT_ROOT = "/home/pi5/Pictures"
LED_1_GPIO_PIN = 16
LED_2_GPIO_PIN = 26
CAMERA_PREVIEW_SIZE = (640, 480)
CAMERA_EXPOSURE_US = 20_000
CAMERA_COLOUR_GAINS = (1.8, 1.3)
ILLUMINATION_STABILIZATION_SECONDS = 2
NUMBER_OF_TUBES = 5
BASELINE_FRAME_COUNT = 5
NEGATIVE_CONTROL_INDEX = 0  # Tube 1; Python uses zero-based indexing.

# Seed coordinates are stored as [x, y]. They were originally selected in
# MATLAB, so one is subtracted before indexing a NumPy image array.
TUBE_SEED_POINTS = (
    (524, 929),
    (902, 779),
    (1316, 914),
    (1586, 776),
    (1937, 920),
)
HSV_TOLERANCE = 30 / 255.0
HSV_NEIGHBORHOOD_SIZE = 15
SMOOTHING_WINDOW_SIZE = 4


def switch_mode():
    """Switch between the two illumination channels."""
    global active_led
    if led1.is_lit:
        led1.off()
        led2.on()
        active_led = led2
    elif led2.is_lit:
        led2.off()
        led1.on()
        active_led = led1
    else:
        led1.on()  # Default to turning on LED1 if both are off
        led2.off()
        active_led = led1


class MonitorWorker(QThread):
    """Capture a time-lapse image series without performing image analysis."""
    update_result = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, frames, sleep_time, new_folder, gap, picam2, led1):
        super().__init__()
        self.frames = frames
        self.sleep_time = sleep_time
        self.new_folder = new_folder
        self.gap = gap
        self.picam2 = picam2
        self.led1 = led1
        self._running = True

    def run(self):
        exposure_delay = ILLUMINATION_STABILIZATION_SECONDS
        for i in range(self.frames):
            if not self._running:
                break
            self.led1.on()
            time.sleep(exposure_delay)
            image_file = os.path.join(self.new_folder, f"{i + 1}.jpg")
            cfg = self.picam2.create_still_configuration()
            self.picam2.switch_mode_and_capture_file(cfg, image_file)
            self.led1.off()
            self.update_result.emit(f"Captured frame {i + 1}/{self.frames}. Waiting for {self.gap} minutes...")
            time.sleep(self.sleep_time)
        self.finished.emit()

    def stop(self):
        self._running = False


# ==========================================================
# SAVED-IMAGE ANALYSIS WORKER
# Processes an existing image folder with the exact same analyzer used by
# real-time monitoring. It runs in a QThread so the GUI remains responsive.
# ==========================================================
class AnalysisWorker(QThread):
    """Analyze saved images using the same analyzer as real-time monitoring.

    The images are processed sequentially with ``RealTimeAnalyzer`` in natural
    filename order. Therefore, the separate analysis button uses the same tube
    segmentation, fallback masks, yellowing-index extraction, baseline
    correction, negative-control correction, smoothing, CSV output, Step 9
    plot, and final Excel export as the real-time acquisition workflow.
    """
    update_result = pyqtSignal(str)
    update_plot = pyqtSignal(str)
    analysis_finished = pyqtSignal(str)
    analysis_error = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, image_dir):
        super().__init__()
        self.image_dir = image_dir

    def run(self):
        try:
            image_files = sorted(
                (
                    name for name in os.listdir(self.image_dir)
                    if name.lower().endswith(('.jpg', '.jpeg'))
                ),
                key=natural_sort_key,
            )
            if not image_files:
                raise ValueError("No .jpg or .jpeg images found in the selected folder.")

            analyzer = RealTimeAnalyzer(self.image_dir)
            total_images = len(image_files)

            for image_index, image_name in enumerate(image_files, start=1):
                self.update_result.emit(
                    f"Analyzing saved image {image_index}/{total_images}: {image_name}"
                )
                image_path = os.path.join(self.image_dir, image_name)
                analyzer.process_frame(image_path, image_index)
                self.update_plot.emit(analyzer.running_plot)
                self.progress.emit(image_index, total_images)

            final_excel = analyzer.finalize()
            self.analysis_finished.emit(
                "Saved-image analysis completed using the shared real-time method. "
                f"Plots saved in: {analyzer.plot_folder}. "
                f"Final Excel saved: {final_excel}"
            )
        except Exception as e:
            self.analysis_error.emit(f"Analysis error: {str(e)}")


def natural_sort_key(text):
    """Return a key that sorts names containing numbers in natural order."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]


def moving_average_omitnan(data, window_size=SMOOTHING_WINDOW_SIZE):
    """Apply a centered moving average while ignoring missing values."""
    import numpy as np

    out = np.full_like(data, np.nan, dtype=float)
    n_rows, n_cols = data.shape
    half_left = (window_size - 1) // 2
    half_right = window_size // 2

    for c in range(n_cols):
        for r in range(n_rows):
            start = max(0, r - half_left)
            end = min(n_rows, r + half_right + 1)
            out[r, c] = np.nanmean(data[start:end, c])
    return out


# The saved-image and real-time workflows intentionally share the same
# RealTimeAnalyzer class below. Keeping one analysis implementation prevents
# the two GUI methods from producing different numerical results or outputs.


# ==========================================================
# SHARED IMAGE-ANALYSIS ENGINE
# Used by both "Analysis" and "Real-Time Monitoring + Analysis".
# It updates cumulative values and saves one bar plot for every frame.
# ==========================================================
class RealTimeAnalyzer:
    """Analyze each captured frame and maintain cumulative experiment results."""
    def __init__(self, image_dir):
        import numpy as np
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import cv2

        self.image_dir = image_dir
        self.n_tubes = NUMBER_OF_TUBES
        self.seed_points = np.asarray(TUBE_SEED_POINTS, dtype=int)
        self.tolerance = HSV_TOLERANCE
        self.neighborhood_size = HSV_NEIGHBORHOOD_SIZE
        self.negative_tube = NEGATIVE_CONTROL_INDEX
        self.fallback_masks = None
        self.avg_rows = []
        self.image_indices = []
        self.colors = plt.cm.tab10(np.linspace(0, 1, self.n_tubes))
        self.disk3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.disk5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))

        # Remove the unused legacy mask-overlay folder if it exists from an older run.
        legacy_overlay_folder = os.path.join(self.image_dir, 'RealTime_Mask_Overlays')
        if os.path.isdir(legacy_overlay_folder):
            shutil.rmtree(legacy_overlay_folder)

        # Create a new results subfolder for every analysis run. This guarantees
        # that plots from earlier runs and earlier frames are never overwritten.
        results_root = os.path.join(self.image_dir, 'RealTime_Analysis_Results')
        os.makedirs(results_root, exist_ok=True)
        run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.plot_folder = os.path.join(results_root, f'Run_{run_stamp}')
        os.makedirs(self.plot_folder, exist_ok=False)

        self.running_csv = os.path.join(self.image_dir, 'RealTime_Yellowing_Index_Running.csv')
        self.running_plot = ''  # Updated to the newest frame-specific plot after each analysis.
        self.final_excel = os.path.join(self.image_dir, 'RealTime_Yellowing_Index_Final.xlsx')

    def process_frame(self, image_path, image_index):
        import numpy as np
        import cv2
        from matplotlib.colors import rgb_to_hsv

        img_bgr = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if img_bgr is None:
            raise ValueError(f"Could not read image: {image_path}")

        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        img_double = img_rgb.astype(np.float32) / 255.0
        hsv_img = rgb_to_hsv(img_double)

        R = img_double[:, :, 0]
        G = img_double[:, :, 1]
        B = img_double[:, :, 2]
        yellowing_index = (G - B) / (R + G + B + np.finfo(float).eps)

        h, w = yellowing_index.shape
        frame_masks = np.zeros((h, w, self.n_tubes), dtype=bool)
        raw_values = np.full(self.n_tubes, np.nan, dtype=float)

        for k in range(self.n_tubes):
            x_matlab, y_matlab = self.seed_points[k]
            x = int(x_matlab) - 1
            y = int(y_matlab) - 1

            if x < 0 or x >= w or y < 0 or y >= h:
                mask = np.zeros((h, w), dtype=bool)
                connected_mask = np.zeros((h, w), dtype=bool)
            else:
                half = self.neighborhood_size // 2
                x1 = max(0, x - half)
                x2 = min(w - 1, x + half)
                y1 = max(0, y - half)
                y2 = min(h - 1, y + half)

                patch = hsv_img[y1:y2 + 1, x1:x2 + 1, :]
                mean_hsv = np.nanmean(patch.reshape(-1, 3), axis=0)

                lower = np.maximum(mean_hsv - self.tolerance, 0)
                upper = np.minimum(mean_hsv + self.tolerance, 1)

                mask = (
                    (hsv_img[:, :, 0] >= lower[0]) & (hsv_img[:, :, 0] <= upper[0]) &
                    (hsv_img[:, :, 1] >= lower[1]) & (hsv_img[:, :, 1] <= upper[1]) &
                    (hsv_img[:, :, 2] >= lower[2]) & (hsv_img[:, :, 2] <= upper[2])
                )

                _, labels = cv2.connectedComponents(mask.astype('uint8'), connectivity=8)
                seed_label = labels[y, x]
                if seed_label > 0:
                    connected_mask = labels == seed_label
                else:
                    connected_mask = np.zeros((h, w), dtype=bool)

            cleaned_mask = cv2.morphologyEx(connected_mask.astype('uint8'), cv2.MORPH_OPEN, self.disk3).astype(bool)
            cleaned_mask = cv2.morphologyEx(cleaned_mask.astype('uint8'), cv2.MORPH_CLOSE, self.disk5).astype(bool)

            final_mask = cleaned_mask.copy()
            if self.fallback_masks is not None and not np.any(final_mask):
                final_mask = self.fallback_masks[:, :, k].copy()

            frame_masks[:, :, k] = final_mask

            if np.any(final_mask):
                raw_values[k] = np.nanmedian(yellowing_index[final_mask])

        self.fallback_masks = frame_masks.copy()
        self.image_indices.append(image_index)
        self.avg_rows.append(raw_values)

        signals = self._compute_running_signals()
        self._save_running_csv(signals)
        self._save_running_plot(signals)

        return signals

    def _compute_running_signals(self):
        import numpy as np

        raw = np.asarray(self.avg_rows, dtype=float)
        n = raw.shape[0]
        baseline_count = min(BASELINE_FRAME_COUNT, n)
        baseline = np.nanmean(raw[:baseline_count, :], axis=0)
        delta = raw - baseline
        negative_signal = delta[:, self.negative_tube].reshape(-1, 1)
        corrected = delta - negative_signal
        smoothed = moving_average_omitnan(corrected, window_size=SMOOTHING_WINDOW_SIZE)

        return {
            'raw': raw,
            'delta': delta,
            'corrected': corrected,
            'smoothed': smoothed,
            'baseline_count': baseline_count,
            'image_indices': list(self.image_indices)
        }

    def _save_running_csv(self, signals):
        import pandas as pd
        import numpy as np

        data = {'Image_Index': signals['image_indices']}
        for name, arr in [
            ('Raw', signals['raw']),
            ('BaselineCorrected', signals['delta']),
            ('NegControlCorrected', signals['corrected']),
            ('SmoothedCorrected', signals['smoothed'])
        ]:
            for k in range(self.n_tubes):
                data[f'{name}_Tube_{k + 1}'] = arr[:, k]

        pd.DataFrame(data).to_csv(self.running_csv, index=False)

    def _save_running_plot(self, signals):
        import numpy as np
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        # Step 9 figure only:
        # Median yellowing index extracted from each tube for the latest frame.
        raw = signals['raw']
        latest_values = raw[-1, :]
        current_frame = signals['image_indices'][-1]

        fig = plt.figure(figsize=(4.8, 3.2), facecolor='w')
        ax = plt.gca()
        x = np.arange(1, self.n_tubes + 1)
        ax.bar(x, latest_values)
        ax.set_xticks(x)
        ax.set_xlabel('Tube Number')
        ax.set_ylabel('Median Yellowing Index')
        ax.set_title(f'Extracted Tube Signal - Frame {current_frame}')
        ax.grid(True, axis='y', alpha=0.35)
        fig.tight_layout()

        # Use a unique filename so every frame result is retained.
        self.running_plot = os.path.join(
            self.plot_folder,
            f'Frame_{int(current_frame):03d}_Median_Yellowing_Index.png'
        )
        fig.savefig(self.running_plot, dpi=130, bbox_inches='tight')
        plt.close(fig)

    def finalize(self):
        import pandas as pd

        signals = self._compute_running_signals()
        columns = [f'Tube_{i}' for i in range(1, self.n_tubes + 1)]
        image_indices = signals['image_indices']

        raw_df = pd.DataFrame(signals['raw'], columns=columns)
        raw_df.insert(0, 'Image_Index', image_indices)

        delta_df = pd.DataFrame(signals['delta'], columns=columns)
        delta_df.insert(0, 'Image_Index', image_indices)

        corrected_df = pd.DataFrame(signals['corrected'], columns=columns)
        corrected_df.insert(0, 'Image_Index', image_indices)

        smoothed_df = pd.DataFrame(signals['smoothed'], columns=columns)
        smoothed_df.insert(0, 'Image_Index', image_indices)

        with pd.ExcelWriter(self.final_excel, engine='openpyxl') as writer:
            raw_df.to_excel(writer, sheet_name='Raw_Yellowing_Index', index=False)
            delta_df.to_excel(writer, sheet_name='Baseline_Corrected', index=False)
            corrected_df.to_excel(writer, sheet_name='Neg_Control_Corrected', index=False)
            smoothed_df.to_excel(writer, sheet_name='Smoothed_Corrected', index=False)

        return self.final_excel


class RealTimeMonitorWorker(QThread):
    """Capture and analyze frames sequentially in a background thread."""
    update_result = pyqtSignal(str)
    update_plot = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, frames, sleep_time, new_folder, gap, picam2, led1):
        super().__init__()
        self.frames = frames
        self.sleep_time = sleep_time
        self.new_folder = new_folder
        self.gap = gap
        self.picam2 = picam2
        self.led1 = led1
        self._running = True

    def run(self):
        try:
            analyzer = RealTimeAnalyzer(self.new_folder)
            exposure_delay = ILLUMINATION_STABILIZATION_SECONDS
            completed_any_frame = False

            for i in range(self.frames):
                if not self._running:
                    break

                frame_number = i + 1
                self.update_result.emit(f"Capturing frame {frame_number}/{self.frames}...")

                self.led1.on()
                time.sleep(exposure_delay)
                image_file = os.path.join(self.new_folder, f"{frame_number}.jpg")
                cfg = self.picam2.create_still_configuration()
                self.picam2.switch_mode_and_capture_file(cfg, image_file)
                self.led1.off()

                self.update_result.emit(f"Frame {frame_number}/{self.frames} captured. Analyzing now...")
                analysis_start = time.time()
                signals = analyzer.process_frame(image_file, frame_number)
                analysis_seconds = time.time() - analysis_start
                completed_any_frame = True

                current_raw = signals['raw'][-1, :]
                current_smoothed = signals['smoothed'][-1, :]

                if len(signals['image_indices']) < BASELINE_FRAME_COUNT:
                    baseline_msg = f"Collecting baseline frames ({len(signals['image_indices'])}/{BASELINE_FRAME_COUNT})."
                else:
                    baseline_msg = f"Baseline locked from first {BASELINE_FRAME_COUNT} frames."

                base_text_lines = [
                    f"Frame {frame_number}/{self.frames} analyzed in {analysis_seconds:.1f} sec. {baseline_msg}",
                    "Raw YI: " + ", ".join([f"T{k + 1}={current_raw[k]:.4f}" for k in range(5)]),
                    "Smoothed corrected: " + ", ".join([f"T{k + 1}={current_smoothed[k]:.4f}" for k in range(5)])
                ]
                base_text = "\n".join(base_text_lines)
                self.update_result.emit(base_text)
                self.update_plot.emit(analyzer.running_plot)
                self.progress.emit(frame_number, self.frames)

                if i < self.frames - 1:
                    remaining = int(self.sleep_time)
                    while remaining > 0 and self._running:
                        if remaining >= 60:
                            wait_msg = f"Next frame in {remaining // 60} min {remaining % 60} sec."
                            step = min(30, remaining)
                        else:
                            wait_msg = f"Next frame in {remaining} sec."
                            step = min(5, remaining)
                        self.update_result.emit(base_text + "\n" + wait_msg)
                        time.sleep(step)
                        remaining -= step

            if completed_any_frame:
                final_excel = analyzer.finalize()
                if self._running:
                    self.finished.emit(f"Real-time monitoring and analysis completed. Final Excel saved: {final_excel}")
                else:
                    self.finished.emit(f"Real-time monitoring stopped. Partial Excel saved: {final_excel}")
            else:
                self.finished.emit("Real-time monitoring stopped before any frame was completed.")

        except Exception as e:
            self.led1.off()
            self.error.emit(f"Real-time analysis error: {str(e)}")

    def stop(self):
        self._running = False


def hide_preview_window():
    global preview_window
    try:
        if preview_window is not None and preview_window.isVisible():
            preview_window.hide()
    except Exception:
        pass

def continuous_monitoring():
    """Start time-lapse acquisition using the duration and interval fields."""
    hide_preview_window()
    capture_image_button.setEnabled(False)
    continuous_monitoring_button.setEnabled(False)
    analysis_button.setEnabled(False)
    real_time_monitoring_button.setEnabled(False)
    try:
        capture_duration = float(duration.text())
        capture_gap = float(gap.text())
        sleep_time = int(round(capture_gap * 60))  # Convert to seconds
        frames = int(round(capture_duration / capture_gap))
        output_name = input_name.text()

        if output_name:
            new_folder = os.path.join(OUTPUT_ROOT, output_name)
            os.makedirs(new_folder, exist_ok=True)

            # Start the worker thread
            global monitor_worker
            monitor_worker = MonitorWorker(frames, sleep_time, new_folder, capture_gap, picam2, active_led)
            monitor_worker.update_result.connect(results.setText)
            monitor_worker.finished.connect(lambda: [
                results.setText("Continuous monitoring completed."),
                capture_image_button.setEnabled(True),
                continuous_monitoring_button.setEnabled(True),
                analysis_button.setEnabled(True),
                real_time_monitoring_button.setEnabled(True),
                stop_real_time_button.setEnabled(False)
            ])
            monitor_worker.start()
        else:
            results.setText("Please enter an experiment name.")
            capture_image_button.setEnabled(True)
            continuous_monitoring_button.setEnabled(True)
            analysis_button.setEnabled(True)
            real_time_monitoring_button.setEnabled(True)
    except Exception as e:
        results.setText(f"Error: {str(e)}")
        capture_image_button.setEnabled(True)
        continuous_monitoring_button.setEnabled(True)
        analysis_button.setEnabled(True)
        real_time_monitoring_button.setEnabled(True)


def save_image():
    """Capture and save one still image using the current experiment name."""
    hide_preview_window()
    output_name = input_name.text()
    if not output_name:
        file_name_caption.setText("Please enter an experiment name.")
        return

    capture_image_button.setEnabled(False)
    try:
        image_file = os.path.join(OUTPUT_ROOT, f"{output_name}.jpg")
        cfg = picam2.create_still_configuration()
        picam2.switch_mode_and_capture_file(cfg, image_file)
        results.setText(f"Image saved: {image_file}")
    except Exception as e:
        results.setText(f"Capture error: {str(e)}")
    finally:
        capture_image_button.setEnabled(True)


class PreviewWindow(QWidget):
    """Display a lightweight live camera preview in a separate window."""
    def __init__(self, camera):
        super().__init__()
        self.camera = camera
        self.setWindowTitle("Camera Preview")

        self.preview_label = QLabel("Preview starting...")
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setFixedSize(500, 375)
        self.preview_label.setStyleSheet("border: 1px solid gray;")

        close_button = QPushButton("Close Preview")
        close_button.clicked.connect(self.hide)

        preview_layout = QVBoxLayout()
        preview_layout.addWidget(self.preview_label)
        preview_layout.addWidget(close_button)
        self.setLayout(preview_layout)
        self.resize(540, 430)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_preview_frame)

    def showEvent(self, event):
        self.timer.start(150)  # update preview about 6-7 frames/second
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        self.hide()
        event.ignore()

    def update_preview_frame(self):
        try:
            frame = self.camera.capture_array()
            if frame is None:
                return

            if frame.ndim == 2:
                qimg = QImage(frame.data, frame.shape[1], frame.shape[0], frame.strides[0], QImage.Format_Grayscale8)
            else:
                # The camera is configured as RGB888, so this should display correctly.
                h, w, ch = frame.shape
                if ch >= 3:
                    rgb = frame[:, :, :3].copy()
                    qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
                else:
                    return

            pixmap = QPixmap.fromImage(qimg)
            self.preview_label.setPixmap(
                pixmap.scaled(500, 375, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            )
        except Exception as e:
            self.preview_label.setText(f"Preview error:\n{str(e)}")


def show_preview():
    global preview_window
    try:
        if preview_window is None:
            preview_window = PreviewWindow(picam2)
        preview_window.show()
        preview_window.raise_()
        preview_window.activateWindow()
    except Exception as e:
        results.setText(f"Preview error: {str(e)}")


def real_time_monitoring():
    """Start combined time-lapse acquisition and frame-by-frame analysis."""
    hide_preview_window()
    capture_image_button.setEnabled(False)
    continuous_monitoring_button.setEnabled(False)
    analysis_button.setEnabled(False)
    real_time_monitoring_button.setEnabled(False)
    preview_button.setEnabled(False)
    stop_real_time_button.setEnabled(True)

    try:
        capture_duration = float(duration.text())
        capture_gap = float(gap.text())
        sleep_time = int(round(capture_gap * 60))
        frames = int(round(capture_duration / capture_gap))
        output_name = input_name.text().strip()

        if not output_name:
            results.setText("Please enter an experiment name.")
            enable_all_main_buttons()
            return

        new_folder = os.path.join(OUTPUT_ROOT, output_name)
        os.makedirs(new_folder, exist_ok=True)

        global real_time_worker
        real_time_worker = RealTimeMonitorWorker(frames, sleep_time, new_folder, capture_gap, picam2, active_led)
        real_time_worker.update_result.connect(results.setText)
        real_time_worker.update_plot.connect(update_analysis_plot)
        real_time_worker.progress.connect(update_frame_status)
        real_time_worker.finished.connect(real_time_done)
        real_time_worker.error.connect(real_time_failed)
        real_time_worker.start()

    except Exception as e:
        results.setText(f"Error: {str(e)}")
        enable_all_main_buttons()


def stop_real_time():
    global real_time_worker
    try:
        if real_time_worker is not None:
            real_time_worker.stop()
            results.setText("Stopping real-time monitoring after the current step...")
    except Exception as e:
        results.setText(f"Stop error: {str(e)}")

def enable_all_main_buttons():
    preview_button.setEnabled(True)
    capture_image_button.setEnabled(True)
    continuous_monitoring_button.setEnabled(True)
    analysis_button.setEnabled(True)
    real_time_monitoring_button.setEnabled(True)
    stop_real_time_button.setEnabled(False)

def real_time_done(message):
    results.setText(message)
    enable_all_main_buttons()


def real_time_failed(message):
    results.setText(message)
    enable_all_main_buttons()


def update_frame_status(current_frame, total_frames):
    """Display analysis progress in the dedicated GUI status line."""
    frame_status_label.setText(f"Frame {current_frame}/{total_frames} analyzed")


def update_analysis_plot(plot_path):
    if os.path.exists(plot_path):
        pixmap = QPixmap(plot_path)
        if not pixmap.isNull():
            analysis_plot_label.setPixmap(
                pixmap.scaled(430, 320, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            )

def run_analysis():
    """Analyze an existing experiment folder selected by experiment name."""
    output_name = input_name.text().strip()
    if not output_name:
        results.setText("Please enter the folder name first.")
        return

    image_dir = os.path.join(OUTPUT_ROOT, output_name)
    if not os.path.isdir(image_dir):
        results.setText(f"Folder not found: {image_dir}")
        return

    capture_image_button.setEnabled(False)
    continuous_monitoring_button.setEnabled(False)
    analysis_button.setEnabled(False)
    real_time_monitoring_button.setEnabled(False)
    results.setText("Analysis started...")

    global analysis_worker
    analysis_worker = AnalysisWorker(image_dir)
    analysis_worker.update_result.connect(results.setText)
    analysis_worker.update_plot.connect(update_analysis_plot)
    analysis_worker.progress.connect(update_frame_status)
    analysis_worker.analysis_finished.connect(analysis_done)
    analysis_worker.analysis_error.connect(analysis_failed)
    analysis_worker.start()


def analysis_done(message):
    results.setText(message)
    capture_image_button.setEnabled(True)
    continuous_monitoring_button.setEnabled(True)
    analysis_button.setEnabled(True)
    real_time_monitoring_button.setEnabled(True)


def analysis_failed(message):
    results.setText(message)
    capture_image_button.setEnabled(True)
    continuous_monitoring_button.setEnabled(True)
    analysis_button.setEnabled(True)
    real_time_monitoring_button.setEnabled(True)



# Initialize gpiozero LED objects
led1 = LED(LED_1_GPIO_PIN)
led2 = LED(LED_2_GPIO_PIN)
active_led = led1  # default light used for capture/monitoring

# Setup application
os.makedirs(OUTPUT_ROOT, exist_ok=True)
app = QApplication([])
picam2 = Picamera2()

# Configure the camera
preview_config = picam2.create_preview_configuration(main={"size": CAMERA_PREVIEW_SIZE, "format": "RGB888"})
picam2.configure(preview_config)
picam2.set_controls({"ExposureTime": CAMERA_EXPOSURE_US})
picam2.set_controls({"AwbMode": 0, "ColourGains": CAMERA_COLOUR_GAINS})

preview_window = None

# Create GUI widgets
# The camera preview is now opened only when needed using the Preview button.
# This keeps the main window small for the Raspberry Pi display.
preview_button = QPushButton("Preview")
switch_mode_button = QPushButton("Toggle Lights")
continuous_monitoring_button = QPushButton("Continuous Monitoring")
capture_image_button = QPushButton("Capture Image")
analysis_button = QPushButton("Analysis")
real_time_monitoring_button = QPushButton("Real-Time Monitoring + Analysis")
stop_real_time_button = QPushButton("Stop Real-Time")
stop_real_time_button.setEnabled(False)
duration_caption = QLabel("Duration (minutes):")
duration = QLineEdit()
duration.setText("30")
gap_caption = QLabel("Gap (minutes):")
gap = QLineEdit()
gap.setText("30")
frame_status_label = QLabel("Frame 0/0 analyzed")
file_name_caption = QLabel("Enter File Name:")
input_name = QLineEdit()
results = QLabel("")
results.setWordWrap(True)
results.setMaximumHeight(105)
analysis_plot_label = QLabel("Results will appear here")
analysis_plot_label.setFixedSize(430, 320)
analysis_plot_label.setAlignment(QtCore.Qt.AlignCenter)
analysis_plot_label.setStyleSheet("border: 1px solid gray; font-size: 11px; background: white;")

# Keep buttons compact for small Raspberry Pi displays
for btn in [preview_button, switch_mode_button, continuous_monitoring_button, capture_image_button, analysis_button, real_time_monitoring_button, stop_real_time_button]:
    btn.setMaximumHeight(28)

# Keep text boxes compact
duration.setMaximumWidth(90)
gap.setMaximumWidth(90)
input_name.setMaximumWidth(230)

window = QWidget()

# Connect signals
preview_button.clicked.connect(show_preview)
switch_mode_button.clicked.connect(switch_mode)
capture_image_button.clicked.connect(save_image)
continuous_monitoring_button.clicked.connect(continuous_monitoring)
analysis_button.clicked.connect(run_analysis)
real_time_monitoring_button.clicked.connect(real_time_monitoring)
stop_real_time_button.clicked.connect(stop_real_time)

# Setup layout
layout_h = QHBoxLayout()
layout_v = QVBoxLayout()
layout_duration_h = QHBoxLayout()
layout_duration_h.addWidget(duration_caption)
layout_duration_h.addWidget(duration)
layout_gap_h = QHBoxLayout()
layout_gap_h.addWidget(gap_caption)
layout_gap_h.addWidget(gap)
layout_v.addWidget(preview_button)
layout_v.addWidget(switch_mode_button)
layout_v.addWidget(continuous_monitoring_button)
layout_v.addWidget(capture_image_button)
layout_v.addWidget(analysis_button)
layout_v.addWidget(real_time_monitoring_button)
layout_v.addWidget(stop_real_time_button)
layout_v.addLayout(layout_duration_h)
layout_v.addLayout(layout_gap_h)
layout_v.addWidget(frame_status_label)
layout_v.addWidget(results)
layout_v.addWidget(file_name_caption)
layout_v.addWidget(input_name)
# No scroll area: compact two-column layout for Raspberry Pi screen.
left_panel = QWidget()
left_panel.setLayout(layout_v)
left_panel.setFixedWidth(320)

right_panel = QVBoxLayout()
step9_title = QLabel("Median Yellowing Index per Tube")
step9_title.setAlignment(QtCore.Qt.AlignCenter)
step9_title.setStyleSheet("font-weight: bold; font-size: 12px;")
right_panel.addWidget(step9_title)
right_panel.addWidget(analysis_plot_label)
right_panel.addStretch()

layout_h.addWidget(left_panel)
layout_h.addLayout(right_panel)
window.setWindowTitle("Thermal Vision")
window.setLayout(layout_h)
window.setFixedSize(790, 470)

# Start application
picam2.start()
window.show()

try:
    app.exec_()
finally:
    # Ensure LEDs are turned off when the application exits
    led1.off()
    led2.off()
