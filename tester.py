import socket
import ipaddress
import json
from datetime import timezone, datetime
import threading



class Logger:
    def __init__(self, filename):
        self.filename = filename
        self.lock = threading.Lock()

    def _getTime(self):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def log(self, type, level, source, text):
        line = f"{self._getTime()}| {type} | {level} | {source} | {text}"
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



class Sensor:
    def __init__(self, type, id):
        self.sensorType = type
        self.sensorId = id
        self.sequence = 0
        self.token = None

    def getType(self):
        return self.sensorType

    def nextSequenceNumber(self):
        self.sequence += 1
        return self.sequence

    def message(self):
        return {"type": "REGISTER", "sequence": self.nextSequenceNumber(), "sensorType": self.sensorType, "sensorId": self.sensorId}



SERVER_IP = "127.0.0.1"
SERVER_PORT = 5500
CLIENT_IP = "0.0.0.0"
CLIENT_PORT = 0


class Client:
    def __init__(self, clientIP = CLIENT_IP, clientPort = CLIENT_PORT):
        self.logger = Logger("tester.log")
        self.sensors = {}
        self.server = (SERVER_IP, SERVER_PORT)
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

    def register(self, sensorType, sensorId):
        key = (sensorType, sensorId)
        if key in self.sensors:
            print("Sensor already registered")
            return
        sensor = Sensor(sensorType, sensorId)
        self.sensors[key] = sensor
        message = sensor.message()
        self.send(message, self.server)
        source = f"TESTER:{sensorType}:{sensorId}"
        self.logger.message(source, "Sent", message)

        reply, _ = self.receive()
        if reply is None:
            return
        self.logger.message(source, "Received", reply)
        if reply.get("type") == "ACK" and reply.get("ack-for") == message["sequence"]:
            sensor.token = reply["token"]
            self.logger.system("INFO", source, f"Registered, token {sensor.token}")
        else:
            del self.sensors[key]

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

    def close(self):
        self.socket.close()



def main():
    client = Client()
    client.register("ThermoNode", "T-01")
    client.register("ThermoNode", "T-02")
    client.register("WindSense", "W-01")

if __name__ == "__main__":
    main()