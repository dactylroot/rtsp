""" Quick script to test an RTSP server. """

import cv2 as cv

vcap = cv.VideoCapture("rtsp://10.38.5.145/ufirststream")

while(1):
    ret, frame = vcap.read()
    cv.imshow('VIDEO', frame)
    cv.waitKey(1)
