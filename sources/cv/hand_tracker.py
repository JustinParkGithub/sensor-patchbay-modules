import cv2
import sys
import time
import select
import mediapipe as mp
from pathlib import Path
from mediapipe.tasks.python import vision
from pprint import pprint

BaseOptions = mp.tasks.BaseOptions
HandLandmarker = vision.HandLandmarker
HandLandmarkerOptions = vision.HandLandmarkerOptions
HandLandmarkerResult = vision.HandLandmarkerResult
VisionRunningMode = vision.RunningMode

# Resolved from this file, not the cwd, so the script runs from anywhere and
# the cv module (__init__.py) can import it.
MODEL_PATH = Path(__file__).resolve().parent / "hand_landmarker.task"

# Tasks API — no mp.solutions, no mp.framework needed
HAND_CONNECTIONS = vision.HandLandmarksConnections.HAND_CONNECTIONS

latest_result = None
_last_log_time = 0.0  # timestamp of the last log, used for throttling


def hand_print_result(result: HandLandmarkerResult, output_image: mp.Image, timestamp_ms: int):
    # This runs on a BACKGROUND THREAD — MediaPipe's LIVE_STREAM mode calls it
    # asynchronously whenever a frame finishes processing. It does NOT run in
    # lockstep with your main loop, which is why we stash the result in a global
    # rather than returning it—there's no caller to return to after the frame has been processed.
    """
    General Idea of what's going on:
    Main loop:    frame1 → frame2 → frame3 → frame4 → frame5 ...
    Background:        [processing frame1...]  → callback fires → [processing frame2...] → callback fires ...
    """
    global latest_result
    latest_result = result  # main loop picks this up on its next iteration
    #NOTE: latest_result has three main attributes: haddedness, hand_landmarks, and hand_world_landmarks.


def make_options(running_mode=VisionRunningMode.LIVE_STREAM, num_hands=2,
                 result_callback=None, model_path=MODEL_PATH):
    """HandLandmarker options. LIVE_STREAM needs a result_callback; VIDEO/IMAGE must not have one."""
    return HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model_path)),
        num_hands=num_hands,
        running_mode=running_mode,
        result_callback=result_callback)


def landmark_points(hand_landmarks):
    """The 21 landmarks of one hand as [(x, y, z), ...] in index order.

    x and y are normalized to [0,1] relative to the image dimensions ((0,0) is top-left,
    not mirrored). z is depth relative to the wrist (negative means closer to the camera),
    on roughly the same scale as x — it is NOT in meters (hand_world_landmarks is).
    """
    return [(lm.x, lm.y, lm.z) for lm in hand_landmarks]


"""
console_quit_requested() uses select() to check if there's input waiting on stdin without
blocking the video loop. If a line equal to q (or EOF, e.g. Ctrl-D) is found, it returns
True and the loop breaks.
"""
def console_quit_requested():
    """Return True if 'q' (followed by Enter) was typed in the console."""
    while select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if not line:  # stdin closed (e.g. EOF)
            return True
        if line.strip().lower() == 'q':
            return True
    return False

def pretty_print_hand_info():
    #Extract wanted information about the hand into a custom dictionary structure
    hand_summary = [
        {
            "hand_index": idx,

            "handedness": hand_list[0].category_name,
            "confidence": round(hand_list[0].score, 2),

            "landmarks_preview": [
                {
                    "point_id": i,
                    "x": round(x, 3),
                    "y": round(y, 3),
                    "z": round(z, 3)
                    #NOTE: see landmark_points() for the coordinate space.
                }
                for i, (x, y, z) in enumerate(landmark_points(latest_result.hand_landmarks[idx]))
                #Loop over every landmark for the current hand, using enumerate to get both the index (point_id) and the landmark data
            ]
        }
        #Loop over each detected hand's handedness info, using the index to also access its landmarks
        for idx, hand_list in enumerate(latest_result.handedness)
    ]

    # Print the structured Python list beautifully
    pprint(hand_summary, sort_dicts=False)

def draw_hand_landmarks(image, hand_landmarks, dot_color=(0, 255, 0), dot_radius=4):
    """Draw landmarks and connections directly with OpenCV. dot_color is BGR."""
    h, w = image.shape[:2] # pull pixel dimensions for denormalizing coords

    #hand_landmarks is a list of NormalizedLandmark objects (x, y, z in 0-1 range)
    #HAND_CONNECTIONS is a list of Connection(start, end) pairs — index pairs into that list
    for connection in HAND_CONNECTIONS:
        start = hand_landmarks[connection.start]  # NormalizedLandmark at the start joint
        end = hand_landmarks[connection.end]       # NormalizedLandmark at the end joint
        x0, y0 = int(start.x * w), int(start.y * h)  # scale from 0-1 to actual pixels
        x1, y1 = int(end.x * w), int(end.y * h)
        cv2.line(image, (x0, y0), (x1, y1), (255, 0, 0), 2)  #draw bone

    #Draw landmark dots
    for lm in hand_landmarks:
        cx, cy = int(lm.x * w), int(lm.y * h)
        cv2.circle(image, (cx, cy), dot_radius, dot_color, -1) #draw joint dot

def main():
    global _last_log_time

    #Variables for Frame rate
    prevTime = 0
    currTime = 0

    with HandLandmarker.create_from_options(make_options(result_callback=hand_print_result)) as landmarker:
        capturer = cv2.VideoCapture(0)
        try:
            while True:
                success, img = capturer.read()
                if not success:
                    print("Failed to capture video")
                    break

                rgb_frame = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

                timestamp_ms = int(time.time() * 1000)
                #Send live image data to perform hand landmarks detection.
                #The results are accessible via the `result_callback` provided in
                #the `HandLandmarkerOptions` object.
                landmarker.detect_async(mp_image, timestamp_ms)

                if latest_result is not None and len(latest_result.handedness) > 0:

                    now = time.monotonic()
                    if (now - _last_log_time) > 0.33:
                        pretty_print_hand_info()
                        _last_log_time = now

                    for hand_landmarks in latest_result.hand_landmarks:
                        draw_hand_landmarks(img, hand_landmarks)

                currTime = time.time()
                fps = 1/(currTime-prevTime)
                prevTime = currTime

                cv2.putText(img, f'FPS: {int(fps)}', (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 255), 4)

                cv2.imshow("Image", img)
                window_quit_requested = cv2.waitKey(1) & 0xFF == ord('q')
                if window_quit_requested or console_quit_requested():
                    break
        finally:
            capturer.release()
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
