class RtpPacket:    
    header = bytearray(HEADER_SIZE)
    
    def __init__(self):
        pass
    
    #--------------
    # TO COMPLETE
    #--------------
        # Fill in the input arguments if needed
    def encode(self, V, P, X, CC, seqNum, M, PT, SSRC, payload):
        """Encode the RTP packet with header fields and payload."""
        timestamp = int(time())
        header = bytearray(HEADER_SIZE)
        # Fill the header bytearray with RTP header fields
        # ...
        header[0] = header[0] | V << 6; 
        header[0] = header[0] | P << 5; 
        header[0] = header[0] | X << 4; 
        header[0] = header[0] | CC; 
        header[1] = header[1] | M << 7; 
        header[1] = header[1] | PT; 
        header[2] = (seqNum >> 8) & 0xFF; 
        header[3] = seqNum & 0xFF; 
        header[4] = (timestamp >> 24) & 0xFF; 
        header[5] = (timestamp >> 16) & 0xFF;
        header[6] = (timestamp >> 8) & 0xFF;
        header[7] = timestamp & 0xFF;
        header[8] = (SSRC >> 24) & 0xFF; 
        header[9] = (SSRC >> 16) & 0xFF;
        header[10] = (SSRC >> 8) & 0xFF;
        header[11] = SSRC & 0xFF

        self.header = header
        
        # Get the payload
        # ...
        self.payload = payload
        
    def decode(self, byteStream):
        """Decode the RTP packet."""
        self.header = bytearray(byteStream[:HEADER_SIZE])
        self.payload = byteStream[HEADER_SIZE:]
    
    def version(self):
        """Return RTP version."""
        return int(self.header[0] >> 6)
    
    def seqNum(self):
        """Return sequence (frame) number."""
        seqNum = self.header[2] << 8 | self.header[3]
        return int(seqNum)
    
    def timestamp(self):
        """Return timestamp."""
        timestamp = self.header[4] << 24 | self.header[5] << 16 | self.header[6] << 8 | self.header[7]
        return int(timestamp)
    
    def payloadType(self):
        """Return payload type."""
        pt = self.header[1] & 127
        return int(pt)
    
    def getPayload(self):
        """Return payload."""
        return self.payload
        
    def getPacket(self):
        """Return RTP packet."""
        return self.header + self.payload