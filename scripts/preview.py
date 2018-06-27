"""
    rtsp.preview

    Using OpenCV/GTk+, preview a stream on your local machine.
"""

import cv2 as _cv2

def preview_stream(stream):
    """ Display stream in an OpenCV window until "q" key is pressed """
    # together with waitkeys later, helps to close the video window effectively
    _cv2.startWindowThread()
    
    for frame in stream.frame_generator():
        if frame is not None:
            _cv2.imshow('Video', frame)
            _cv2.moveWindow('Video',5,5)
        else:
            break
        key = _cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
    _cv2.waitKey(1)
    _cv2.destroyAllWindows()
    _cv2.waitKey(1)