import socket
import ipaddress
import json
from json import JSONDecodeError

CLIENT_IP = "127.0.0.1"
CLIENT_PORT = 5500


class Client:
    def __init__(self, clientIP = CLIENT_IP, clientPort = CLIENT_PORT):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        try:
            ipaddress.ip_address(clientIP)
            self.ip = clientIP
        except ValueError:
            raise ValueError(f"Invalid IP address: {clientIP}")

        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.ip, clientPort))
            self.port = clientPort
        except OSError as error:

            if error.errno in (48, 98, 10048):
                raise ConnectionError(f"port{clientPort} is already occupied") from error
            else:
                self.socket.close()
                raise error


    def send(self, message, destinationAddress):
        self.socket.sendto(json.dumps(message).encode("utf-8"), destinationAddress)

    def receive(self):
        data, address = self.socket.recvfrom(1200)

        try:
            message = json.loads(data.decode("utf-8"))
            if not isinstance(message, dict):
                return None, address

            return message, address
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, address

