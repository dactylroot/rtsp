"""
    RTSP/2.0 Protocol
    spec: https://tools.ietf.org/html/rfc7826

Overview:
    1. Session establishment
        1. setup
            1. socket connection
            2. authentication
        2. presentation description
            1. determine media streams are available
            2. determine media delivery protocol used (media initialization)
            3. determine resource identifiers of the media streams
            4. establish session with specific resources and media
    2. Media Delivery Control
        1. Transmission Control
        2. Synchronization
        3. Termination
"""



#import sys
#import time
from enum import Enum
import socket
from urllib.parse import urlparse


##########################################################
#################   Protocol Constants   #################
##########################################################
HEADER_SIZE = 12
RTSP_VER = "RTSP/2.0"
TRANSPORT = "HTTP"
DEFAULT_PORT = 554

states   = Enum('state'    , "init ready play")
requests = Enum('requests' , "setup play pause teardown")

vendor_paths = { 'axis':'/axis-media/media.amp'
               , 'illustra':'/ufirststream/track1'
               }

USER_AGENT = 'py-rtsp'

##########################################################
#################   Default Server URI   #################
##########################################################
def _cam_uri():
    #return "http://10.38.5.145/uapi-cgi/viewer/snapshot.fcgi?_=1525375407561443"
    return "rtsp://root:irkbidbird@192.168.1.11/axis-media/media.amp"

def printrec(recst):
    """ Pretty-printing rtsp strings"""
    try:
        recst = recst.decode('UTF-8')
    except AttributeError:
        pass
    recs=[ x for x in recst.split('\r\n') if x ]
    for rec in recs:
        print("    "+rec)
    print("\n")

##########################################################
#################          API           #################
##########################################################

class Client:
    """ RTSP requests which serialize to bytestrings for transmission """

    @property
    def server(self):
        return self._server

    @property
    def serverbase(self):
        return "{}://{}".format(self.server.scheme,self.server.hostname)

    @property
    def media(self):
        return "{}://{}{}".format(self.server.scheme,self.server.hostname,self.server.path)
    

    def __init__(self,server_uri=_cam_uri(),verbose=True):
        self.verbose = verbose
        self._server = urlparse(server_uri)

        if not self.server.port:
            self._server = self.server._replace(netloc="{}:{}".format(self.server.netloc,DEFAULT_PORT))

        self.state = states.init
        self.rtsp_seq = 1
        self._connect()

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
        self.sock.connect((self.server.hostname, self.server.port))
        if self.verbose:
            print("Socket connected to \'{}:{}\'.".format(self.server.hostname,self.server.port))

    def sendMessage(self,message,reply_len=4096):
        if self.verbose:
            print("  Sending message to server:")
            printrec(message)

        self.sock.send(message)
        reply = self.sock.recv(reply_len)

        if self.verbose:
            if reply:
                print("  Received reply:")
                printrec(reply)
                #self.parseRtspReply(reply)
            else:
                print("no reply")

        return self.parseResp(reply.decode('UTF-8'))

    def parseResp(self,response):
        resp = response.split('\r\n')
        resp[0] = 'Response: '+resp[0]
        resp = {y[0]:y[1] for y in [x.split(':',maxsplit=1) for x in resp] if len(y) > 1}
        return resp

    def options(self):
        self.rtsp_seq += 1

        request = "OPTIONS {} {}\r\n".format(self.media,RTSP_VER)
        request+= "CSeq: {}\r\n".format(self.rtsp_seq)
        request+= "User-Agent: {}\r\n\r\n".format(USER_AGENT)

        response = self.sendMessage(request.encode('UTF-8'))

        return response

    #### TODO fix the requests below

    def describe(self):
        self.rtsp_seq += 1

        request = "DESCRIBE {} {}\r\n".format(self.media,RTSP_VER)
        request+= "CSeq: {}\r\n".format(self.rtsp_seq)
        request+= "Authorization: Digest username={} realm=\"AXIS_ACCC8EAE5A30\" "
        request+= "User-Agent: {}\r\n".format(USER_AGENT)
        request+= "Accept: application/sdp\r\n\r\n"
        
        resp = self.sendMessage(request.encode('UTF-8'))

        import IPython
        IPython.embed()

    def setup(self,resource_path = "track1"):
        """ SETUP method defined by RTSP Protocol - https://tools.ietf.org/html/rfc7826#section-13.3 """
        self.rtsp_seq += 1

        ## example: SETUP rtsp://example.com/foo/bar/baz.rm RTSP/2.0
        request = "SETUP {} {}\r\n".format(self.server.geturl(),RTSP_VER)
        request+= "CSeq: {}\r\n".format(self.rtsp_seq)
        request+= "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"#,RTP/SAVPF,RTP/AVP;unicast;client_port=5000-5001,RTP/AVP/UDP;unicast;client_port=5000-5001\r\n"
        request+= "Authorization: \"irkbidbird\"\r\n"
        request+= "User-Agent: \"{}\"\r\n\r\n".format(self.server.username)

        reply = self.sendMessage(request.encode('UTF-8'))
        return reply

    def get_session(self):
        self.rtsp_seq += 1

        request = "GET_PARAMETER SESSION\r\n"
        request+= "CSeq: {}\r\n\r\n".format(self.rtsp_seq)
    
        response = self.sendMessage(request.encode('UTF-8'))
        return response.split('\r\n')[3].split(' ')[1]

    def get_parameter(self,parameter):
        self.rtsp_seq += 1

        request = "GET_PARAMETER {}\r\n".format(parameter)
        request+= "CSeq: {}\r\n".format(self.rtsp_seq)
        request+= "Session: {}\r\n".format(self.session)
        request+= "Content-Type: text/parameters\r\n"
        request+= "Content-Length: 15\r\n\r\n"
        #request+= "Transport\r\n\r\n"
        #request+= "jitter\r\n"

        return self.sendMessage(request.encode('UTF-8'))

    def pause(self):
        self.rtsp_seq += 1

        request = "PAUSE rtsp://{}{} {}\r\n".format(self.server,self.stream_path,RTSP_VER)
        request+= "CSeq: {}\r\n".format(self.rtsp_seq)
        request+= "Session: {}\r\n\r\n".format(self.session)

        return self.sendMessage(request.encode('UTF-8'))

def get_resources(client):
    """ Do an RTSP-DESCRIBE request, then parse out available resources from the response """
    resp = client.options()
    #resp = client.describe().split('\r\n')
    #resources = [x.replace('a=control:','') for x in resp if (x.find('control:') != -1 and x[-1] != '*' )]
    #return resources

