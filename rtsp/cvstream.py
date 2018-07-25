### https://github.com/jrosebr1/imutils/blob/master/imutils/video/filevideostream.py

# import the necessary packages
from threading import Thread
import cv2

class VideoSkip:
    def __init__(self, path):
        self.stream = cv2.VideoCapture(path)
        self.Q = None
        self.start()

    def start(self):
        t = Thread(target=self.update, args=())
        t.daemon = True
        self.stopped = False
        t.start()
        return self

    def isOpened(self):
        return not self.stopped

    def preview(self):
        """ Stop flushing the buffer and read the stream here """
        self.stop()

        win_name = 'Frame'
        opening = True
        cv2.namedWindow(win_name, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(win_name,20,20)
        while(self.stream.isOpened() and opening):
            opening, frame = self.stream.read()
            cv2.imshow(win_name,frame)
            if cv2.waitKey(25) & 0xFF == ord('q'):
                opening = False

        cv2.waitKey()
        cv2.destroyAllWindows()
        cv2.waitKey()

        self.start()

    def update(self):
        # keep looping infinitely
        while True:
            # if the thread indicator variable is set, stop the
            # thread
            if self.stopped:
                return

            # read the next frame from the file
            (grabbed, frame) = self.stream.read()

            # if the `grabbed` boolean is `False`, then we have
            # reached the end of the video file
            if not grabbed:
                self.stop()
                return

            # add the frame to the queue
            self.Q = frame

    def read(self):
        return self.Q

    def stop(self):
        # indicate that the thread should be stopped
        self.stopped = True