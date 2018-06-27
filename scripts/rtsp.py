"""
    RTSP/2.0 Protocol
    spec: https://tools.ietf.org/html/rfc7826
"""

import sys
from enum import Enum
import socket
import time
from urllib.parse import urlparse

##########################################################
#################   Protocol Constants   #################
##########################################################
HEADER_SIZE = 12
RTSP_VER = "RTSP/1.0"
TRANSPORT = "HTTP"

states   = Enum('state'    , "init ready play")
requests = Enum('requests' , "setup play pause teardown")


##########################################################
#################   Default Server URI   #################
##########################################################
def _cafe_uri():
    #return "http://10.38.5.145/uapi-cgi/viewer/snapshot.fcgi?_=1525375407561443"
    return "rtsp://10.38.5.145/ufirststream"

def _rtsp_default_port():
    return 554

def printrec(recst):
    """ Pretty-printing rtsp strings
    """
    try:
        recst = recst.decode('UTF-8')
    except AttributeError:
        pass
    recs=[ x for x in recst.split('\r\n') if x ]
    for rec in recs:
        print(rec)
    print("\n")

##########################################################
#################          API           #################
##########################################################

class RTSPConnection:
    """ RTSP requests which serialize to bytestrings for transmission """
    def __init__(self,server_uri=_cafe_uri(),server_port=_rtsp_default_port(),client_port=_rtsp_default_port()):
        resource_uri = urlparse(server_uri)

        self.server = resource_uri.netloc
        self.stream_path = resource_uri.path
        self.server_port = int(server_port)
        self.client_port = int(client_port)

        self.state = states.init
        self.rtsp_seq = 0
        self._connect()
        self.session = self.get_session()

    def __enter__(self,*args,**kwargs):
        """ Returns the object which later will have __exit__ called.
            This relationship creates a context manager. """
        return self

    def __exit__(self, type=None, value=None, traceback=None):
        """ Together with __enter__, allows support for with- clauses. """
        self.sock.__exit__()

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect((self.server, self.server_port))
        print(f"Socket connected to \'{self.server}\'.")

    def sendMessage(self,message,reply_len=4096,verbose=True):
        if verbose:
            print(f"  Sending message to server:\n")
            printrec(message)

        self.sock.send(message)
        reply = self.sock.recv(reply_len)

        if verbose:
            if reply:
                print("  Received reply:\n")
                printrec(reply)
                #self.parseRtspReply(reply)
            else:
                print("no reply")

        return reply.decode('UTF-8')

    def describe(self,verbose=True):
        self.rtsp_seq += 1

        request = f"DESCRIBE rtsp://{self.server}{self.stream_path} {RTSP_VER}\r\n"
        request +=f"CSeq: {self.rtsp_seq}\r\n"
        request +=f"User-Agent: python\r\n"
        request +=f"Accept: application/sdp\r\n\r\n"
        return self.sendMessage(request.encode('UTF-8'),verbose=verbose)

    def options(self):
        self.rtsp_seq += 1

        request = "OPTIONS * RTSP/1.0\r\n"
        request+= f"CSeq: {self.rtsp_seq}\r\n"
        request+= "Require: implicit-play\r\n\r\n"
        
        return self.sendMessage(request.encode('UTF-8'))

    def get_session(self):
        self.rtsp_seq += 1

        request =f"GET_PARAMETER rtsp://{self.server}{self.stream_path} {RTSP_VER}\r\n"
        request+=f"CSeq: {self.rtsp_seq}\r\n\r\n"
    
        response =  self.sendMessage(request.encode('UTF-8'),verbose=False)
        return response.split('\r\n')[3].split(' ')[1]

    def get_parameter(self):
        self.rtsp_seq += 1

        request =f"GET_PARAMETER rtsp://{self.server}{self.stream_path} {RTSP_VER}\r\n"
        request+=f"CSeq: {self.rtsp_seq}\r\n"
        request+=f"Session: {self.session}\r\n"
        request+= "Content-Type: text/parameters\r\n"
        request+= "Content-Length: 15\r\n\r\n"
        #request+= "Transport\r\n\r\n"
        #request+= "jitter\r\n"

        return self.sendMessage(request.encode('UTF-8'))

    def pause(self):
        self.rtsp_seq += 1

        request =f"PAUSE rtsp://{self.server}{self.stream_path} {RTSP_VER}\r\n"
        request+=f"CSeq: {self.rtsp_seq}\r\n"
        request+=f"Session: {self.session}\r\n\r\n"

        return self.sendMessage(request.encode('UTF-8'))

    def setup(self,resource_path = "track1"):
        """ SETUP method defined by RTSP Protocol - https://tools.ietf.org/html/rfc7826#section-13.3 """
        self.rtsp_seq += 1

        uri = '/'.join([s.strip('/') for s in (self.server,self.stream_path,resource_path)])
        
        ## example: SETUP rtsp://example.com/foo/bar/baz.rm RTSP/2.0
        request = f"SETUP rtsp://{uri} {RTSP_VER}\r\n"
        request+= f"CSeq: {self.rtsp_seq}\r\n"
        request+= f"Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"#,RTP/SAVPF,RTP/AVP;unicast;client_port=5000-5001,RTP/AVP/UDP;unicast;client_port=5000-5001\r\n"
        #request+= f"Accept-Ranges: npt, smpte, clock\r\n"
        request+= f"User-Agent: python\r\n\r\n"

        reply = self.sendMessage(request.encode('UTF-8'))
        return reply

def get_resources(connection):
    """ Do an RTSP-DESCRIBE request, then parse out available resources from the response """
    resp = connection.describe(verbose=False).split('\r\n')
    resources = [x.replace('a=control:','') for x in resp if (x.find('control:') != -1 and x[-1] != '*' )]
    return resources

def test():
    with RTSPConnection() as connection:
        resources = get_resources(connection)
        print(f"identified resource path \'{resources[0]}\'")
        connection.setup(resource_path=resources[0])

if __name__=='__main__':
    #with RTSPConnection() as connection:
    #    print(get_resources(connection))
    test()
