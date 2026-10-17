import socket
import ipaddress
import json
from datetime import timezone, datetime
import threading
import secrets


class Logger:
    def __init__(self, filename):
        self.filename = filename
        self.lock = threading.Lock()

    def _getTime(self):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def log(self, type, level, source, text):
        line = f"{self._getTime()}| {type} | {level} | {source} | {text} "
        with self.lock:
            try:
                with open(self.filename, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass



    def system(self, level, source, text):
        self.log("SYSTEM", level, source, text)

    def message(self, source, direction, message):
        self.log("MESSAGE", "INFO", source, f"{direction}: {json.dumps(message, separators=(',', ':'))}")






class Server:
    SERVER_IP = "127.0.0.1"
    SERVER_PORT = 5500
    SENSOR_TYPES = ("ThermoNode", "WindSense", "RainDetect", "AirQuality")

    def __init__(self, serverIP = SERVER_IP, serverPort = SERVER_PORT):
        self.socket =socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.logger = Logger("server.log")
        self.sequence = 0
        self.sessions = {}

        try:
            ipaddress.ip_address(serverIP)
            self.ip = serverIP
        except ValueError:
            raise ValueError(f"Invalid IP address: {serverIP}")

        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.ip, serverPort))
            self.port = serverPort
        except OSError as error :

            if error.errno in (48, 98, 10048):
                raise ConnectionError(f"port{serverPort} is already occupied") from error
            else:
                self.socket.close()
                raise error

    def nextSequenceNumber(self):
        self.sequence += 1
        return self.sequence


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

    def run(self):
        self.logger.system("INFO", "SERVER", f"Server is listening on {self.ip}:{self.port}")
        while True:
            message, address = self.receive()
            if message is None:
                self.logger.system("WARNING", "SERVER", f"Invalid message from {address}")
                continue
            self.logger.message("SERVER", "Received", message)
            self.handleMessage(message, address)



    def handleMessage(self, message, address):
        mType = message.get("type")
        if mType == "REGISTER":
            self.handleReg(message, address)
        elif mType == "DATA":
            self.handleData(message, address)



    def handleReg(self, message, address):
        sensorType = message.get("sensorType")
        sensorId = message.get("sensorId")
        if sensorType not in self.SENSOR_TYPES or not sensorId:
            self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Invalid sensor"}, address)
            return
        for session in self.sessions.values():
            if session["sensorType"] == sensorType and session["sensorId"] == sensorId:
                self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Already registered"},
                           address)
                return
        token = secrets.token_hex(6)
        self.sessions[token] = {"sensorType": message["sensorType"], "sensorId": message["sensorId"], "address": address}
        return self.reply({"type": "ACK", "ack-for": message["sequence"], "token": token }, address)

    def handleData(self, message, address):
        if message.get("token") not in self.sessions:
            self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Invalid token"}, address)

    def reply(self, message, address):
        message["sequence"] = self.nextSequenceNumber()
        self.send(message, address)
        self.logger.message("SERVER", "Sent", message)


    def close(self):
         self.socket.close()




def main():
    server = Server()
    server.run()

if __name__ == "__main__":
    main()