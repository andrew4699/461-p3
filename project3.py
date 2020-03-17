import socket
import struct
import sys
import threading


banned_file = open('serverlist.txt', 'r')
banned_list = banned_file.readlines()


# helper function that splits the string on the first CRLF or LF found.
# returns None if none were found.
def splitLine(s):
    crlf = s.find(b'\r\n')
    lf = s.find(b'\n')
    if crlf == -1:
        if lf != -1:
            return s[:lf], s[lf+1:]
        else:
            return None
    else:
        if lf == crlf + 1:
            return s[:crlf], s[crlf+2:]
        else:
            return s[:lf], s[lf+1:]

# helper function that returns the corrected header
def preprocessLine(line):
    if line is None:
        return None
    elif line.lower().find(b'proxy-connection: keep-alive') != -1:
        return b'Proxy-Connection: close\r\n'
    elif line.lower().find(b'connection: keep-alive') != -1:
        return b'Connection: close\r\n'
    else:
        return line + b'\r\n'


# buffer for received messages from a socket. supports sending bytes,
# reading a line, and reading the next 1024 bytes.
class SockBuffer():
    def __init__(self, sock):
        self.sock = sock
        self.buf = b''

    def close(self):
        self.sock.close()

    # sends the given bytes object to the socket.
    def sendBytes(self, s):
        self.sock.send(s)

    # PRIVATE
    # reads next 1024 bytes from sock or less, returning it as a bytes object.
    # returns None and closes sock on socket error.
    def readBytes(self):
        try:
            retval = self.sock.recv(1024)
        except:
            return None
        #print(f"read: {retval}")
        return retval

    # pops next line from the sock. lines are either separated by
    # a '\n' or a '\r\n'. If the sock is out of data to send, then
    # returns all buffered data.
    # returns None and closes sock on socket error.
    def getNextLine(self):
        result = splitLine(self.buf)
        while result is None:
            next_bits = self.readBytes()
            if next_bits is None:
                return None

            if len(next_bits) == 0:
                return None

            self.buf += next_bits
            result = splitLine(self.buf)

        #print(repr(result))
        #print(repr(self.buf))
        self.buf = result[1]
        return result[0]

    # pops at most 1024 bytes from the sock and returns as a string.
    # returns None on sock error.
    def getNext1024(self):
        s = self.readBytes()
        if s is None:
            return None

        retval = self.buf + s
        self.buf = b''
        return retval



# executes a round of http (request or response) from the SockBuffer
# sender_buf to the SockBuffer receiver_buf.
class HttpRound(threading.Thread):
    def __init__(self, s_b, r_b, i_r):
        threading.Thread.__init__(self, daemon=True)
        self.s_b, self.r_b, self.i_r = s_b, r_b, i_r
        #print(f"created httpround with {i_r}")



    def run(self):
        sender_buf, receiver_buf, is_request = self.s_b, self.r_b, self.i_r
        phase = 1 if is_request else 2 # denotes the current phase
        init_phase, header_phase, body_phase = 1,2,3

        backlog = None
        is_connect = False

        while True:
            # read the relevant bytes for the next iteration
            next_bytes = sender_buf.getNext1024() if phase == body_phase \
                                else preprocessLine(sender_buf.getNextLine())
            if next_bytes is None:
                # socket error
                return

            if phase == init_phase:
                # We are in the initial phase of an HTTP request.
                # Remember three things - the initial line, whether
                # the protocol is HTTPS or HTTP, and whether the request
                # is a CONNECT request. Always move on to the header phase.
                print('>>>' + next_bytes[:-2].decode('utf-8'))

                backlog = next_bytes
                is_https = next_bytes.split()[1][0:5] == b'https'
                is_connect = next_bytes[:7] == b'CONNECT'
                phase = header_phase
            elif phase == header_phase:
                # We are in the header phase of the HTTP message (or in the init
                # phase of an HTTP response, which is the same as a header for our
                # purposes).

                # First send the current header to the receiver if it is determined;
                # else save the current header as backlog
                if backlog is None:
                    receiver_buf.sendBytes(next_bytes)
                else:
                    backlog += next_bytes

                # If the header is a Host header, then connect the receiver_buf and
                # send the backlog of previous headers and init line.
                if next_bytes[0:5].lower() == b'host:' and is_request:
                    # host header, connect the receiver_buf socket and send backlog
                    val = next_bytes.split()[1].split(b':')

                    address = val[0]
                    for ip in banned_list:
                        if ip in address:
                            break
                        if address in ip:
                            break
                        if ip in socket.gethostbyname(address):
                            break
                        if socket.gethostbyname(address) in ip:
                            break
                    # determine the port
                    if len(val) == 1:
                        port = 443 if is_https else 80
                    else:
                        port = int(val[1])

                    # Determine if the connection was successful
                    connect_success = False
                    try:
                        receiver_buf.sock.connect((address, port))
                        connect_success = True
                    except:
                        if not is_connect:
                            return

                    if not is_connect:
                        receiver_buf.sendBytes(backlog)
                        backlog = None

                        response_round = HttpRound(receiver_buf, sender_buf, False)
                        response_round.start()

                if len(next_bytes) <= 2:
                    # received an empty line; signifies end of headers
                    if is_connect:
                        break
                    phase = body_phase
            elif phase == body_phase:
                # We are in the body phase, where we simply pass on the next line
                if len(next_bytes) == 0:
                    break

                receiver_buf.sendBytes(next_bytes)

        if is_connect:
            if connect_success:
                send_http_response(sender_buf, b'200', b'OK')
                Tunnel(sender_buf, receiver_buf).start()
                Tunnel(receiver_buf, sender_buf).start()
            else:
                send_http_response(sender_buf, b'502', b'Bad Gateway')

def send_http_response(sock, status_code, status_message):
    response = b'HTTP/1.1 ' + status_code + b' ' + status_message + b'\r\n\r\n'
    sock.sendBytes(response)

# Forwards all messages received by src to dest
class Tunnel(threading.Thread):
    # src and dest are SockBuffers
    def __init__(self, src, dest):
        threading.Thread.__init__(self, daemon=True)
        self.src = src
        self.dest = dest

    def run(self):
        #print('start tunnel')

        while True:
            message_from_src = self.src.getNext1024()
            if message_from_src is None:
                print("closing tunnel. error")
                self.stop()
                return

            if len(message_from_src) > 0:
                #print(f"forwarding {len(message_from_src)} bytes from {self.src}...")
                self.dest.sendBytes(message_from_src)
            else:
                #print("closing tunnel.")
                return

    def stop(self):
        self.src.close()
        self.dest.close()


def main():
    port = int(sys.argv[1])

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((socket.gethostname(), port))

    s.listen(5)

    while True:
        (clientsocket, address) = s.accept()
        #print('accepted tcp')
        requester = SockBuffer(clientsocket)
        responder = SockBuffer(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        thread = HttpRound(requester, responder, True)
        thread.start()



main()