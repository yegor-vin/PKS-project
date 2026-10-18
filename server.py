import socket
import ipaddress
import json
from datetime import timezone, datetime
import threading
import secrets
import time


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
    SENSOR_FIELDS = {
        "ThermoNode": ("temperature", "humidity", "dew_point", "pressure"),
        "WindSense": ("wind_speed", "wind_gust", "wind_direction", "turbulence"),
        "RainDetect": ("rainfall", "soil_moisture", "flood_risk", "rain_duration"),
        "AirQuality": ("co2", "ozone", "air_quality_index"),
    }

    def __init__(self, serverIP = SERVER_IP, serverPort = SERVER_PORT):
        self.socket =socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.logger = Logger("server.log")
        self.lock = threading.Lock()
        self.running = False
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
        with self.lock:
            self.sequence += 1
            return self.sequence

    def start(self):
        self.running = True
        self.logger.system("INFO", "SERVER", f"Server listening on {self.ip}:{self.port}")
        threading.Thread(target=self.run, daemon=True).start()




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
        while True:
            try:
                message, address = self.receive()
            except OSError:
                if not self.running:
                    break
                continue
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
        if sensorType not in self.SENSOR_FIELDS or not sensorId:
            self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Invalid sensor"}, address)
            return
        with self.lock:
            duplicate = any(s["sensorType"] == sensorType and s["sensorId"] == sensorId
                            for s in self.sessions.values())
            if not duplicate:
                token = secrets.token_hex(6)
                self.sessions[token] = {"sensorType": sensorType, "sensorId": sensorId,
                                        "address": address, "lastData": time.monotonic()}
        if duplicate:
            self.reply({"type": "REJECT", "ack-for": message.get("sequence"), "reason": "Already registered"}, address)
            return
        self.logger.system("INFO", "SERVER", f"Sensor registered: {sensorType}:{sensorId}, token {token}")


        return self.reply({"type": "ACK", "ack-for": message["sequence"], "token": token }, address)

    def handleData(self, message, address):
       sequence = message.get("sequence")
       with self.lock:
           session = self.sessions.get(message.get("token"))

       if session is None:
           self.reply({"type": "REJECT", "ack-for": sequence, "reason": "Invalid token"}, address)
           return


       if message.get("sensorId") != session["sensorId"]:
           self.reply({"type": "REJECT", "ack-for": sequence, "reason": "token doesn't match sensor Id"}, address)
           return

       fields = self.SENSOR_FIELDS[session["sensorType"]]
       if any(field not in message for field in fields):
           self.reply({"type": "REJECT", "ack-for": sequence, "reason": "Missing values"}, address)
           return

       with self.lock:
           session["lastData"] = time.monotonic()
           session["address"] = address
       values = {field: message[field] for field in fields}
       print(f"[DATA] {session['sensorType']}:{session['sensorId']} {values}")
       self.reply({"type": "ACK", "ack-for": sequence}, address)


    def reply(self, message, address):
        message["sequence"] = self.nextSequenceNumber()
        self.send(message, address)
        self.logger.message("SERVER", "Sent", message)


    def close(self):
        self.running = False
        self.socket.close()
        self.logger.system("INFO", "SERVER", "Server stopped")




def main():
    server = Server()
    server.start()
    print(f"Server listening on {server.ip}:{server.port}. Press Enter to stop.")
    input()
    server.close()


if __name__ == "__main__":
    main()