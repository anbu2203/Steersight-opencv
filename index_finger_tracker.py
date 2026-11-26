"""
Index finger tracker (Python + OpenCV + MediaPipe)

Setup:   pip install opencv-python mediapipe==0.10.14 numpy
Optional (for mouse control):  pip install pyautogui
Run:     python index_finger_tracker.py

Keys:    M = toggle mouse control   T = toggle trail   C = clear trail   Q / Esc = quit

Features
  - Index fingertip tracking (landmark 8) with One Euro smoothing: steady when still, fast when moving
  - Gesture detection: POINT, OPEN palm, FIST, PINCH (thumb + index)
  - Pinch = click / drag (with hysteresis so it doesn't flicker)
  - Swipe detection (LEFT / RIGHT / UP / DOWN)
  - Fingertip trail, speed readout, handedness, FPS
  - Optional real mouse control through an active zone mapped to your screen
  - Recovers cleanly when the hand leaves and re-enters the frame
"""
import math
import time
from collections import deque

import cv2
import mediapipe as mp
import numpy as np

try:
    import pyautogui
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True      # slam the mouse into a screen corner to abort
    HAVE_PYAUTOGUI = True
except Exception:
    HAVE_PYAUTOGUI = False

# ---------- Settings (tweak these) ----------
CAM_W, CAM_H = 640, 480
MIN_CUTOFF = 1.0          # lower = smoother when still
BETA = 0.02               # higher = less lag when moving fast
ZONE = (0.15, 0.85, 0.15, 0.80)   # active zone (x0, x1, y0, y1) as fraction of frame
PINCH_ON, PINCH_OFF = 0.25, 0.40  # pinch thresholds (ratio to hand size)
EXTEND_RATIO = 1.15       # finger counts as extended if tip is this much farther than PIP
TRAIL_LEN = 40
SWIPE_WINDOW = 0.30       # seconds
SWIPE_DIST = 0.35         # fraction of frame width/height covered in the window
SWIPE_COOLDOWN = 0.8

mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils
mp_style = mp.solutions.drawing_styles


class OneEuro:
    """One Euro filter: adaptive low-pass that kills jitter without adding lag."""

    def __init__(self, min_cutoff=MIN_CUTOFF, beta=BETA, d_cutoff=1.0):
        self.mc, self.beta, self.dc = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self.x_prev, self.dx_prev, self.t_prev = None, 0.0, None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.x_prev is None:
            self.x_prev, self.t_prev = x, t
            return x
        dt = max(t - self.t_prev, 1e-3)
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(self.dc, dt)
        dx_hat = a_d * dx + (1 - a_d) * self.dx_prev
        a = self._alpha(self.mc + self.beta * abs(dx_hat), dt)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx_hat, t
        return x_hat


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def finger_extended(pts, tip, pip):
    """Rotation-proof: extended if the tip is clearly farther from the wrist than the PIP joint."""
    return dist(pts[0], pts[tip]) > dist(pts[0], pts[pip]) * EXTEND_RATIO


def classify(pts, pinched):
    idx = finger_extended(pts, 8, 6)
    mid = finger_extended(pts, 12, 10)
    ring = finger_extended(pts, 16, 14)
    pinky = finger_extended(pts, 20, 18)
    if pinched:
        return "PINCH"
    if idx and not (mid or ring or pinky):
        return "POINT"
    if idx and mid and ring and pinky:
        return "OPEN"
    if not (idx or mid or ring or pinky):
        return "FIST"
    return "OTHER"


def main():
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
    if not cap.isOpened():
        raise SystemExit("Could not open the camera.")

    hands = mp_hands.Hands(max_num_hands=1, model_complexity=1,
                           min_detection_confidence=0.7, min_tracking_confidence=0.6)

    fx, fy = OneEuro(), OneEuro()
    trail = deque(maxlen=TRAIL_LEN)
    history = deque()                  # (t, x, y) for swipe detection
    pinched, was_pinched = False, False
    mouse_on, show_trail = False, True
    swipe_text, swipe_until, last_swipe = "", 0.0, 0.0
    prev_t, fps = time.time(), 0.0
    was_seen = False
    screen_w, screen_h = pyautogui.size() if HAVE_PYAUTOGUI else (1920, 1080)

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)                       # mirror view
        h, w = frame.shape[:2]
        res = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        now = time.time()
        fps = 0.9 * fps + 0.1 / max(now - prev_t, 1e-3)
        prev_t = now

        # active-zone rectangle
        zx0, zx1, zy0, zy1 = int(ZONE[0] * w), int(ZONE[1] * w), int(ZONE[2] * h), int(ZONE[3] * h)
        cv2.rectangle(frame, (zx0, zy0), (zx1, zy1), (90, 90, 90), 1)

        gesture, label, conf = "NO HAND", "-", 0.0
        speed = 0.0
        seen = bool(res.multi_hand_landmarks)

        if seen:
            lms = res.multi_hand_landmarks[0]
            mp_draw.draw_landmarks(frame, lms, mp_hands.HAND_CONNECTIONS,
                                   mp_style.get_default_hand_landmarks_style(),
                                   mp_style.get_default_hand_connections_style())
            cls = res.multi_handedness[0].classification[0]
            label, conf = cls.label, cls.score

            pts = [(p.x * w, p.y * h) for p in lms.landmark]

            if not was_seen:                             # hand just appeared: avoid a jump from old state
                fx.reset(); fy.reset(); trail.clear(); history.clear()
            sx, sy = fx(pts[8][0], now), fy(pts[8][1], now)   # smoothed fingertip (pixels)

            # pinch with hysteresis, normalised by hand size
            palm = dist(pts[0], pts[9]) + 1e-6
            ratio = dist(pts[4], pts[8]) / palm
            if not pinched and ratio < PINCH_ON:
                pinched = True
            elif pinched and ratio > PINCH_OFF:
                pinched = False
            gesture = classify(pts, pinched)

            trail.append((int(sx), int(sy)))
            history.append((now, sx, sy))
            while history and now - history[0][0] > SWIPE_WINDOW:
                history.popleft()

            # speed (px/s) over the recent window
            if len(history) >= 2:
                t0, x0, y0 = history[0]
                speed = dist((x0, y0), (sx, sy)) / max(now - t0, 1e-3)

            # swipe detection (not while pinching/dragging)
            if (gesture != "PINCH" and len(history) >= 3 and now - last_swipe > SWIPE_COOLDOWN):
                _, x0, y0 = history[0]
                dx, dy = sx - x0, sy - y0
                if abs(dx) > SWIPE_DIST * w and abs(dx) > 2 * abs(dy):
                    swipe_text = "SWIPE RIGHT" if dx > 0 else "SWIPE LEFT"
                elif abs(dy) > SWIPE_DIST * h and abs(dy) > 2 * abs(dx):
                    swipe_text = "SWIPE DOWN" if dy > 0 else "SWIPE UP"
                else:
                    swipe_text = ""
                if swipe_text:
                    last_swipe, swipe_until = now, now + 0.8
                    history.clear()

            # normalised 0..1 position inside the active zone
            nx = float(np.clip((sx / w - ZONE[0]) / (ZONE[1] - ZONE[0]), 0, 1))
            ny = float(np.clip((sy / h - ZONE[2]) / (ZONE[3] - ZONE[2]), 0, 1))

            # optional real mouse control
            if mouse_on and HAVE_PYAUTOGUI:
                if gesture in ("POINT", "PINCH"):
                    pyautogui.moveTo(int(nx * (screen_w - 1)), int(ny * (screen_h - 1)), _pause=False)
                if pinched and not was_pinched:
                    pyautogui.mouseDown()
                elif was_pinched and not pinched:
                    pyautogui.mouseUp()
            was_pinched = pinched

            # draw fingertip
            tip = (int(sx), int(sy))
            colour = (0, 0, 255) if gesture == "PINCH" else (0, 255, 255)
            cv2.circle(frame, tip, 12, colour, -1)
            cv2.circle(frame, tip, 16, (255, 255, 255), 2)
            cv2.putText(frame, f"({tip[0]}, {tip[1]})  norm ({nx:.2f}, {ny:.2f})",
                        (tip[0] + 20, tip[1] - 10), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (255, 255, 255), 1, cv2.LINE_AA)
        else:
            if was_pinched and mouse_on and HAVE_PYAUTOGUI:
                pyautogui.mouseUp()                      # never leave the button stuck down
            pinched = was_pinched = False
            history.clear()
        was_seen = seen

        # trail
        if show_trail:
            for i in range(1, len(trail)):
                a = i / len(trail)
                cv2.line(frame, trail[i - 1], trail[i], (0, int(255 * a), int(255 * (1 - a))),
                         max(1, int(5 * a)), cv2.LINE_AA)

        # HUD
        lines = [f"FPS {fps:4.0f}",
                 f"Gesture: {gesture}",
                 f"Hand: {label} ({conf:.2f})",
                 f"Speed: {speed:5.0f} px/s",
                 f"Mouse: {'ON' if mouse_on else 'off'}"
                 + ("" if HAVE_PYAUTOGUI else " (pip install pyautogui)")]
        for i, s in enumerate(lines):
            cv2.putText(frame, s, (10, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2, cv2.LINE_AA)
        if now < swipe_until:
            cv2.putText(frame, swipe_text, (w // 2 - 110, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 165, 255), 3, cv2.LINE_AA)
        cv2.putText(frame, "[M] mouse  [T] trail  [C] clear  [Q] quit", (10, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)

        cv2.imshow("Index Finger Tracker", frame)
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord("q")):
            break
        elif key == ord("m"):
            if HAVE_PYAUTOGUI:
                mouse_on = not mouse_on
                if not mouse_on and was_pinched:
                    pyautogui.mouseUp()
        elif key == ord("t"):
            show_trail = not show_trail
        elif key == ord("c"):
            trail.clear()

    if mouse_on and HAVE_PYAUTOGUI:
        pyautogui.mouseUp()
    cap.release()
    hands.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
