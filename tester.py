import socket
import ipaddress
import json
from datetime import timezone, datetime
import threading
import random


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

sensorSpecification = {
    "ThermoNode": [
        ("temperature", -50.0, 60.0, 1),
        ("humidity", 0.0, 100.0, 1),
        ("dew_point", -50.0, 60.0, 1),
        ("pressure", 800.0, 1100.0, 2),
    ],
    "WindSense": [
        ("wind_speed", 0.0, 50.0, 1),
        ("wind_gust", 0.0, 70.0, 1),
        ("wind_direction", 0, 359, 0),
        ("turbulence", 0.0, 1.0, 1),
    ],
    "RainDetect": [
        ("rainfall", 0.0, 500.0, 1),
        ("soil_moisture", 0.0, 100.0, 1),
        ("flood_risk", 0, 3, 0),
        ("rain_duration", 0, 60, 0),
    ],
    "AirQuality": [
        ("co2", 300, 5000, 0),
        ("ozone", 0.0, 500.0, 1),
        ("air_quality_index", 0, 500, 0),
    ],
}


class Sensor:
    def __init__(self, type, id):
        self.sensorType = type
        self.sensorId = id
        self.stop = threading.Event()
        self.token = None
        self.send = True

    def getType(self):
        return self.sensorType

    def source(self):
        return f"TESTER:{self.sensorType}:{self.sensorId}"


    def registerMessage(self, sequence):
        return {"type": "REGISTER", "sequence": sequence, "sensorType": self.sensorType, "sensorId": self.sensorId}

    def generateValues(self):
        values = {}
        for name, low, high, decimals in sensorSpecification[self.sensorType]:
            if decimals == 0:
                values[name] = random.randint(low, high)
            else:
                values[name] = round(random.uniform(low, high), decimals)
        return values

    def dataMessage(self, sequence, values=None):
        if values is None:
            values = self.generateValues()
        message = {"type": "DATA", "sequence": sequence, "sensorId": self.sensorId, "token": self.token}
        message.update(values)
        return message



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
        self.sequence = 0
        self.pending = {}
        self.running = False
        self.lock = threading.Lock()

        try:
            ipaddress.ip_address(clientIP)
            self.ip = clientIP
        except ValueError:
            raise ValueError(f"Invalid IP address: {clientIP}")

        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.ip, clientPort))
            self.port = self.socket.getsockname()[1]
        except OSError as error:

            if error.errno in (48, 98, 10048):
                raise ConnectionError(f"port{clientPort} is already occupied") from error
            else:
                self.socket.close()
                raise error

    def nextSequenceNumber(self):
        with self.lock:
            self.sequence += 1
            return self.sequence



    def register(self, sensorType, sensorId):
        if sensorType not in sensorSpecification:
            print("Unknown sensor type")
            return
        key = (sensorType, sensorId)
        with self.lock:
            if key in self.sensors:
                print("Sensor already registered")
                return
            sensor = Sensor(sensorType, sensorId)
            self.sensors[key] = sensor
        message = sensor.registerMessage(self.nextSequenceNumber())
        self.sendForSensor(sensor, message)

    def start(self):
        self.running = True
        threading.Thread(target=self.listening, daemon=True).start()

    def listening(self):
        while self.running:
            try:
                reply, _ = self.receive()
            except OSError:

                if not self.running:
                    break
                continue
            if reply is None:
                self.logger.system("WARNING", "TESTER", "Invalid message received")
                continue
            self.handleReply(reply)

    def handleReply(self, reply):
        with self.lock:
            entry = self.pending.pop(reply.get("ack-for"), None)
        if entry is None:
            self.logger.message("TESTER", "Received (unexpected)", reply)
            return

        sensor, original = entry
        self.logger.message(sensor.source(), "Received", reply)
        replyType = reply.get("type")

        if original["type"] == "REGISTER":
            if replyType == "ACK" and reply.get("token"):
                sensor.token = reply["token"]
                self.logger.system("INFO", sensor.source(), f"Registered, token {sensor.token}")
                self.startSending(sensor)

            else:
                self.logger.system("WARNING", sensor.source(),
                                   f"Registration rejected: {reply.get('reason')}")
                with self.lock:
                    self.sensors.pop((sensor.sensorType, sensor.sensorId), None)

        elif original["type"] == "DATA":
            if replyType == "ACK":
                self.logger.system("INFO", sensor.source(), f"Data {original['sequence']} acknowledged")
            else:
                self.logger.system("WARNING", sensor.source(),
                                   f"Data {original['sequence']} rejected: {reply.get('reason')}")


    def startSending(self, sensor):
        threading.Thread(target=self.sendLoop, args=(sensor,), daemon=True).start()

    def sendLoop(self, sensor):
        while not sensor.stop.wait(10):
            if sensor.send and sensor.token:
                self.sendData(sensor)

    def sendData(self, sensor, values=None):
        message = sensor.dataMessage(self.nextSequenceNumber(), values)
        self.sendForSensor(sensor, message)


    def sendForSensor(self, sensor, message):
        with self.lock:
            self.pending[message["sequence"]] = (sensor, message)
        try:
            self.send(message, self.server)
        except OSError as error:
            self.logger.system("ERROR", sensor.source(), f"Send failed: {error}")
            return
        self.logger.message(sensor.source(), "Sent", message)

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
        self.running = False
        with self.lock:
            for sensor in self.sensors.values():
                sensor.stop.set()
        self.socket.close()


def main():
    client = Client()
    client.start()
    client.register("ThermoNode", "T-01")
    client.register("ThermoNode", "T-02")
    client.register("WindSense", "W-01")
    client.register("RainDetect", "R-01")
    client.register("AirQuality", "A-01")
    input()
    client.close()

if __name__ == "__main__":
    main()