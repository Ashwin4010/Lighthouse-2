"""Full toolchain to extract moving objects from a video, add them to a database
or compare with existing objects from the database.
"""
from __future__ import print_function

import logging
import os
import subprocess
import sys
import time
import cv2
import numpy


import config
import audioutils
from camera import Camera
from eventloop import EventLoop
from image_database import ImageDatabase
from image_description import ImageDescription, TooFewFeaturesException

# Define base and sounds folder paths.
BASE_PATH = os.path.dirname(__file__)
SOUNDS_PATH = os.path.join(BASE_PATH, 'sounds')

# Define some sounds that we will be playing.
START_RECORDING_TONE = audioutils.makebeep(800, .2)
STOP_RECORDING_TONE = audioutils.makebeep(400, .2)
CHIRP = audioutils.makebeep(600, .05)
SHUTTER_TONE = None

DEBUG = False

db = None
camera = None
options = config.get_config()

logger = logging.getLogger(__name__)

# Keep track of whether we're currently busy or not
busy = False

# The EventLoop object
eventloop = None


def get_sound(name):
    return os.path.join(SOUNDS_PATH, name)


def take_picture():
    audioutils.playAsync(SHUTTER_TONE)
    return camera.capture()


def pick_only_accurate_matches(matches):
    # Loop though the scores until we find one that is bigger than the
    # threshold, or significantly bigger than the best score and then return
    # all the matches above that one.
    retval = []
    best_score = matches[0][0] if len(matches) > 0 else 0
    if best_score >= options.matching_score_threshold:
        retval.append(matches[0])
        for match in matches[1:]:
            if match[0] >= options.matching_score_threshold and match[0] >= \
                            best_score * options.matching_score_ratio:
                retval.append(match)
            else:
                break

    return retval


def match_item(frames):
    matches = []

    # Image with the larger number of matches.
    image = None

    # We'll take up to this many pictures in order to find match.
    for image in frames:
        # FIXME: That's bad, we should check all frames we have before we fail.
        try:
            matches = db.match(image)
        except TooFewFeaturesException:
            continue
        else:
            # Once we find first accurate match, let's stop trying to find more.
            if len(matches) > 0 and matches[0][0] >= \
                    options.matching_score_threshold:
                break

    if len(matches) == 0:
        logger.info("Too few features.")
        audioutils.playfile(get_sound('nothing_recognized.wav'))
        return

    accurate_matches = pick_only_accurate_matches(matches)

    if len(accurate_matches) == 0:
        audioutils.playfile(get_sound('noitem.wav'))
        logger.debug("No accurate matches found (closest match has score %s).",
                     matches[0][0] if len(matches) > 0 else 0)
    elif len(accurate_matches) == 1:
        (score, item) = accurate_matches[0]
        logger.debug("Found one match with score '%s'.", score)
        audioutils.playfile(item.audio_filename())
    else:
        audioutils.playfile(get_sound('multipleitems.wav'))
        logger.debug("Found several matches with the following scores:")
        for (score, match) in accurate_matches:
            logger.debug("Score: %s", score)
            audioutils.playfile(match.audio_filename())
            time.sleep(0.2)

    if options.log_path and len(matches) > 0:
        start = time.time()

        # Store both original photo and photo with keypoints.
        file_id = time.strftime("%Y%m%dT%H%M%S")
        filename = "{}/{}-match.png".format(options.log_path, file_id)
        filename_original = "{}/{}.png".format(options.log_path, file_id)

        (score, item) = matches[0]
        match_image = item.draw_match(image)
        cv2.putText(match_image, "Score: {}".format(score), (10, 25),
                    cv2.FONT_HERSHEY_PLAIN, 1, (255, 255, 255))
        cv2.imwrite(filename, match_image)
        cv2.imwrite(filename_original, image)
        logger.debug("Match photo saved in %s", time.time() - start)


def capture_moving_objects(expected_number_of_frames):
    op_start = time.clock()
    background_subtractor = cv2.createBackgroundSubtractorKNN()

    previous_frame = None
    captured_frames = []
    stable_captured_frames = 0
    surface = options.video_width * options.video_height
    resample_factor = options.video_resample_factor
    expected_number_of_frames += options.motion_skip_frames

    # Capture images. We expect that the user is moving the object in front of
    # the camera. Continue filming until motion stabilizes.
    while (len(captured_frames) < expected_number_of_frames
           or
           stable_captured_frames < options.motion_stability_duration):
        frame = camera.capture()
        captured_frames.append(frame)

        # We're running in limited memory, make sure that we're not keeping too
        # many frames in memory.
        if len(captured_frames) > expected_number_of_frames:
            captured_frames = captured_frames[1:]

        # Check stability.
        if previous_frame is not None:
            diff = cv2.norm(previous_frame, frame)
            if diff <= surface * options.motion_stability_factor:
                # Ok, not too much movement between the last two images, we
                # might be stabilizing.
                stable_captured_frames += 1
            else:
                stable_captured_frames = 0

        previous_frame = frame

    object_frames = []

    # Now proceed with background subtraction.
    for idx, frame in enumerate(captured_frames):
        downsampled_frame = cv2.resize(frame, (0, 0),
                                       fx=resample_factor,
                                       fy=resample_factor)
        downsampled_noisy_mask = cv2.bitwise_and(
            background_subtractor.apply(downsampled_frame), 255)

        # Experience shows that the background subtractor needs a few frames
        # before it produces anything usable.
        if idx < options.motion_skip_frames:
            continue

        # Ok, at this stage, the background subtraction should be bootstrapped.
        # We can make use of `downsampled_noisy_mask`.
        downsampled_height, downsampled_width = downsampled_noisy_mask.shape[:2]

        # Approximate everything by polygons, removing the smallest polygons.
        # This has the double effect of:
        # - getting rid of all contours that are too small;
        # - restoring missing pixels inside the moving object.
        _, contours, _ = cv2.findContours(downsampled_noisy_mask,
                                          cv2.RETR_EXTERNAL,
                                          cv2.CHAIN_APPROX_NONE)

        downsampled_bw_mask = numpy.zeros((downsampled_height,
                                           downsampled_width),
                                          numpy.uint8)
        is_empty = True
        for cnt in contours:
            area = cv2.contourArea(cnt)
            fraction = area / (resample_factor * resample_factor * surface)
            if fraction > options.motion_discard_small_polygons:
                is_empty = False
                hull = cv2.convexHull(cnt)
                cv2.fillPoly(downsampled_bw_mask, [hull], 255, 8)

        if is_empty:
            # This image isn't really useful, let's throw it away.
            continue

        # Now that all the sophisticated computations are done, upsample the
        # mask and use it to extract the object, with transparency.
        bw_mask = cv2.resize(downsampled_bw_mask, (0, 0),
                             fx=1/resample_factor,
                             fy=1/resample_factor)
        split_1, split_2, split_3 = cv2.split(frame)
        object_frame = cv2.merge([split_1, split_2, split_3, bw_mask])
        object_frames.append(object_frame)

    logger.info("After background subtraction, I have %d objects.",
                len(object_frames))

    op_stop = time.clock()
    logger.info("Image capture took %d s", (op_stop - op_start))
    return object_frames


def capture_everything():
    """Image acquisition strategy: just take a bunch of pictures, don't attempt
    to remove the background."""

    captured_frames = []
    while len(captured_frames) < options.matching_n_frames:
        frame = camera.capture()
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
        captured_frames.append(frame)

    audioutils.playAsync(SHUTTER_TONE)

    return captured_frames

_full_image_for_capture_by_unhiding = None
def capture_by_unhiding():
    """Image acquisition strategy: on the first call, take a picture, assume it's
    the full image, including the object. On a second call, wait until the image
    has stabilized, then capture a second image, assume it's the image without
    the object. Compute the difference between both images, use it to remove
    the background."""

    global _full_image_for_capture_by_unhiding
    if _full_image_for_capture_by_unhiding is None:
        logger.debug("Capturing full image.")

        # Warm up the camera and let it do its white balance while
        # we give the instructions
        camera.start()
        audioutils.playfile(get_sound('register_step1.wav'))

        # First, capture the full image.
        _full_image_for_capture_by_unhiding = camera.capture()
        audioutils.play(SHUTTER_TONE)

        audioutils.playfile(get_sound('register_step2.wav')) # Lasts ~4 seconds.
        # Continue after a moment
        return 0 # Seconds.

    # At this stage, we have waited a few seconds, let's resume capture.
    logger.debug("Capturing object.")

    full_image = _full_image_for_capture_by_unhiding
    _full_image_for_capture_by_unhiding = None
    stable_captured_frames = 0
    surface = options.video_width * options.video_height

    previous_frame = full_image
    frame = None
    while stable_captured_frames < options.motion_stability_duration:
        frame = camera.capture()
        diff = cv2.norm(previous_frame, frame)
        previous_frame = frame

        logger.debug("Frame difference is %d/%d", diff,
                     surface * options.motion_stability_factor)

        if diff <= surface * options.motion_stability_factor:
            # Ok, not too much movement between the last two images, we
            # might be stabilizing.
            stable_captured_frames += 1
        else:
            stable_captured_frames = 0

    # At this stage, `full_image` should contain the background + object
    # and `frame` should contain the background without the object.
    # Let's compute a difference.

    # We start by resampling and blurring to remove as many small differences
    # as possible.
    resample_factor = options.video_resample_factor
    downsampled_frame = cv2.resize(frame, (0, 0),
                                   fx=resample_factor,
                                   fy=resample_factor)
    downsampled_frame = cv2.GaussianBlur(downsampled_frame,
                                         (options.motion_blur_radius,
                                          options.motion_blur_radius),
                                         0)
    downsampled_full_image = cv2.resize(full_image, (0, 0),
                                        fx=resample_factor,
                                        fy=resample_factor)
    downsampled_full_image = cv2.GaussianBlur(downsampled_full_image,
                                              (options.motion_blur_radius,
                                               options.motion_blur_radius),
                                              0)


    b1, g1, r1 = cv2.split(downsampled_frame)
    b2, g2, r2 = cv2.split(downsampled_full_image)
    downsampled_noisy_mask = cv2.absdiff(b1, b2) // 3 \
        + cv2.absdiff(g1, g2) // 3 \
        + cv2.absdiff(r1, r2) // 3
    _, downsampled_noisy_mask = cv2.threshold(downsampled_noisy_mask, 0, 255,
                                              (cv2.THRESH_BINARY_INV |
                                               cv2.THRESH_OTSU))

    # Get rid of noise in the mask.
    _, contours, _ = cv2.findContours(downsampled_noisy_mask, cv2.RETR_EXTERNAL,
                                      cv2.CHAIN_APPROX_NONE)

    downsampled_height, downsampled_width = downsampled_noisy_mask.shape[:2]
    surface = downsampled_height * downsampled_width
    downsampled_denoised_bw_mask = numpy.zeros((downsampled_height,
                                                downsampled_width),
                                               numpy.uint8)

    logger.debug("Number of contours found: %d", len(contours))

    for cnt in contours:
        area = cv2.contourArea(cnt)
        fraction = area / (resample_factor * resample_factor * surface)
        if fraction > options.motion_discard_small_polygons:
            hull = cv2.convexHull(cnt)
            cv2.fillPoly(downsampled_denoised_bw_mask, [hull], 255, 8)

    # Extract object from background.
    denoised_bw_mask = cv2.resize(downsampled_denoised_bw_mask, (0, 0),
                                  fx=1/resample_factor,
                                  fy=1/resample_factor,
                                  interpolation=cv2.INTER_NEAREST)

    split_1, split_2, split_3 = cv2.split(full_image)
    object_frame = cv2.merge([split_1, split_2, split_3, denoised_bw_mask])

    audioutils.playAsync(SHUTTER_TONE)

    # Useful for debugging.
    if DEBUG:
        cv2.imwrite("/tmp/full_image.png", full_image)
        cv2.imwrite("/tmp/frame.png", frame)
        cv2.imwrite("/tmp/object.png", object_frame)
        cv2.imwrite("/tmp/noisy_mask.png", downsampled_noisy_mask)
        cv2.imwrite("/tmp/downsampled_denoised_bw_mask.png", downsampled_denoised_bw_mask)
        cv2.imwrite("/tmp/denoised_bw_mask.png", denoised_bw_mask)

    return [object_frame]


def capture_by_subtracting():
    """Image acquisition strategy: expect the user to move the object in front
    of the camera. Once the object has stopped moving, use background
    subtraction to remove the background."""

    op_start = time.clock()
    background_subtractor = cv2.createBackgroundSubtractorKNN()

    previous_frame = None
    captured_frames = []
    stable_captured_frames = 0
    surface = options.video_width * options.video_height
    resample_factor = options.video_resample_factor
    expected_number_of_frames = options.motion_skip_frames + \
                                options.matching_n_frames

    audioutils.playfile(get_sound('shake_it.wav'))

    # Capture images. We expect that the user is moving the object in front of
    # the camera. Continue filming until motion stabilizes.
    while (len(captured_frames) < expected_number_of_frames
           or
           stable_captured_frames < options.motion_stability_duration):
        frame = camera.capture()
        captured_frames.append(frame)

        # We're running in limited memory, make sure that we're not keeping too
        # many frames in memory.
        if len(captured_frames) > expected_number_of_frames:
            captured_frames = captured_frames[1:]

        # Check stability.
        if previous_frame is not None:
            diff = cv2.norm(previous_frame, frame)
            if diff <= surface * options.motion_stability_factor:
                # Ok, not too much movement between the last two images, we
                # might be stabilizing.
                stable_captured_frames += 1
            else:
                stable_captured_frames = 0

        previous_frame = frame

    audioutils.playAsync(SHUTTER_TONE)

    object_frames = []

    # Now proceed with background subtraction.
    for idx, frame in enumerate(captured_frames):
        downsampled_frame = cv2.resize(frame, (0, 0),
                                       fx=resample_factor,
                                       fy=resample_factor)
        downsampled_subtraction = background_subtractor.apply(downsampled_frame)
        _, downsampled_noisy_mask = cv2.threshold(downsampled_subtraction,
                                                  200, 255, cv2.THRESH_BINARY)

        # Experience shows that the background subtractor needs a few frames
        # before it produces anything usable.
        if idx < options.motion_skip_frames:
            continue

        # Ok, at this stage, the background subtraction should be bootstrapped.
        # We can make use of `downsampled_noisy_mask`.
        downsampled_height, downsampled_width = downsampled_noisy_mask.shape[:2]

        # Approximate everything by polygons, removing the smallest polygons.
        # This has the double effect of:
        # - getting rid of all contours that are too small;
        # - restoring missing pixels inside the moving object.
        _, contours, _ = cv2.findContours(downsampled_noisy_mask,
                                          cv2.RETR_EXTERNAL,
                                          cv2.CHAIN_APPROX_NONE)

        downsampled_bw_mask = numpy.zeros((downsampled_height,
                                           downsampled_width),
                                          numpy.uint8)
        is_empty = True
        for cnt in contours:
            area = cv2.contourArea(cnt)
            fraction = area / (resample_factor * resample_factor * surface)
            if fraction > options.motion_discard_small_polygons:
                is_empty = False
                hull = cv2.convexHull(cnt)
                cv2.fillPoly(downsampled_bw_mask, [hull], 255, 8)

        if is_empty:
            # This image isn't really useful, let's throw it away.
            logger.info("Can't find any moving object on frame %d", idx)
            continue

        # Now that all the sophisticated computations are done, upsample the
        # mask and use it to extract the object, with transparency.
        bw_mask = cv2.resize(downsampled_bw_mask, (0, 0),
                             fx=1/resample_factor,
                             fy=1/resample_factor,
                             interpolation=cv2.INTER_NEAREST)
        split_1, split_2, split_3 = cv2.split(frame)
        object_frame = cv2.merge([split_1, split_2, split_3, bw_mask])
        object_frames.append(object_frame)

        if DEBUG:
            cv2.imwrite("/tmp/object-%d.png" % idx, object_frame)

    logger.info("After background subtraction, I have %d objects.",
                len(object_frames))

    op_stop = time.clock()
    logger.info("Image capture took %d s",
                (op_stop - op_start))
    return object_frames


def record_new_item(frames):
    # Make several pictures and choose the frame with the highest number of
    # features. Sometimes camera needs more time to automatically adjust itself
    # for the current light conditions.
    best_description = None
    best_image = None

    for image in frames:
        try:
            description = ImageDescription.from_image(image)

            if best_description is None or len(best_description.features) < \
                    len(description.features):
                best_description = description
                best_image = image
        except TooFewFeaturesException:
            logger.info("Too few features in the frame.")

    if best_description is None:
        audioutils.playfile(get_sound('nothing_recognized.wav'))
        return

    audio = None
    while audio is None:
        audioutils.playfile(get_sound('afterthetone.wav'))
        audioutils.play(START_RECORDING_TONE)
        audio = audioutils.record(min_duration=2,
                                  max_duration=options.max_record_time,
                                  silence_threshold=options.silence_threshold,
                                  silence_factor=options.silence_factor)
        audioutils.play(STOP_RECORDING_TONE)
        if len(audio) < 800:  # if we got less than 50ms of sound
            audio = None
            audioutils.playfile(get_sound('nosound.wav'))

    item = db.add(best_image, audio, best_description)
    logger.info("Added image in %s.", item.dirname)
    audioutils.playfile(get_sound('registered.wav'))
    audioutils.play(audio)


# Play a sound that indicates that Lighthouse is ready for another button press.
def ready():
    global busy
    busy = False
    audioutils.play(CHIRP)


def capture_frames_then(callback):
    global busy
    busy = True

    if options.motion_background_removal_strategy == "keep-everything":
        frames = capture_everything()
    elif options.motion_background_removal_strategy == "now-you-see-me":
        frames = capture_by_unhiding()
    elif options.motion_background_removal_strategy == "moving-object":
        frames = capture_by_subtracting()
    else:
        logger.exception("Unexpected capture strategy %s",
                         options.motion_background_removal_strategy)
        raise Exception

    if isinstance(frames, list):
        # `frames` actually contains frames
        callback(frames)
        eventloop.later(ready, 0.5)
    else:
        # `frames` is actually a delay before we should
        # try again
        eventloop.later(lambda: capture_frames_then(callback), frames)


def button_handler(event, pin):
    # If we're still processing some other event, ignore this one
    if busy:
        logger.debug('ignoring event %s', event)
        return

    logger.debug("Pin #%s is activated by '%s' event", pin, event)

    if event == 'press':
        camera.start()
    elif event == 'click':
        capture_frames_then(match_item)
    elif event == 'longpress':
        capture_frames_then(record_new_item)


def keyboard_handler(key=None):
    if busy:
        logger.debug('ignoring key %s', key)
        return

    if key == 'R' or key == 'r':
        capture_frames_then(record_new_item)
    elif key == 'M' or key == 'm':
        capture_frames_then(match_item)
    elif key == 'Q' or key == 'q':
        sys.exit(0)
    else:
        print('Enter R to record a new item'
              ' or M to match an item'
              ' or Q to quit')


def main():
    # Load the database of items we know about.
    global db
    db = ImageDatabase(options)

    # Initialize the camera object we'll use to take pictures.
    global camera
    camera = Camera(options.video_source,
                    options.video_width,
                    options.video_height,
                    options.video_fps)

    with open(get_sound('shutter.raw'), 'rb') as f:
        global SHUTTER_TONE
        SHUTTER_TONE = f.read()

    # Set up the audio devices if they are configured
    if options.audio_out_device:
        audioutils.ALSA_SPEAKER = options.audio_out_device
    if options.audio_in_device:
        audioutils.ALSA_MICROPHONE = options.audio_in_device

    # If log path is set, make sure the corresponding directory exists.
    if options.log_path and not os.path.isdir(options.log_path):
        os.makedirs(options.log_path)

    # If --web-server was specified, run a web server in a separate process
    # to expose the files in that directory.
    # Note that we're using port 80, assuming we'll always run as root.
    if options.web_server:
        subprocess.Popen(['python', '-m', 'SimpleHTTPServer', '80'],
                         cwd=options.web_server_root)

    # Monitor the button for events
    global eventloop
    eventloop = EventLoop()
    eventloop.monitor_gpio_button(options.gpio_pin, button_handler,
                                  doubleclick_speed=0)

    # If you don't have a button, use --cmd-ui to monitor the keyboard instead.
    if options.cmd_ui:
        # Print instructions.
        keyboard_handler()
        # Monitor it on the event loop.
        eventloop.monitor_console(keyboard_handler, prompt="Command: ")

    # Let the user know we're ready
    ready()

    # Run the event loop forever
    eventloop.loop()

if __name__ == '__main__':
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception:
        logger.exception("Program crashed")
        raise
