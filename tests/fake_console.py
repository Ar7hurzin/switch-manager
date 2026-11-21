"""Scripted serial console imitating a VRP 3 / Comware 3 switch (no hardware).

SYNTHETIC fixtures: the S3026C and 3Com outputs below follow the VRP 3 /
Comware 3 manuals and must be replaced by real captures (python start.py
capture) once the equipment is available.
"""
from __future__ import annotations

S3900_VERSION = """VRP (R) Software, Version 3.10(CN), RELEASE 0002
Quidway 3900-SI 28-Port uptime is 0 week,0 day,0 hour,32 minutes
Hardware Version is SI_REV.C"""

S3026C_VERSION = """Huawei Versatile Routing Platform Software
VRP (R) Software, Version 3.10, Release 0035
Copyright (c) 1998-2005 HUAWEI TECH CO., LTD. All rights reserved.
Quidway S3026C uptime is 0 week, 0 day, 1 hour, 2 minutes
Hardware Version is REV.B
Bootrom Version is 120"""

S3026C_BRIEF = "\n".join(
    ["The brief information of interface(s) under bridge mode:",
     "Interface            Link      Speed        Duplex   Link-type  PVID"]
    + [f"Ethernet0/{n:<14}{'UP' if n in (1, 2) else 'DOWN':<10}100M         full     access     1" for n in range(1, 25)]
    + ["GigabitEthernet1/1  DOWN      auto         auto     access     1",
       "GigabitEthernet2/1  DOWN      auto         auto     access     1"])

S3026C_PORT = """Ethernet0/1 current state : UP
 Description : uplink
 The Maximum Transmit Unit is 1500
 PVID: 10
 Port link-type: access
  Tagged   VLAN ID : none
  Untagged VLAN ID : 10
 Last 300 seconds input:  2 packets/sec 250 bytes/sec
 Last 300 seconds output:  1 packets/sec 125 bytes/sec"""

S3026C_VLANS = "Now, the following VLAN exist(s):\n 1(default), 10"

COM4500_VERSION = """3Com Corporation
Switch 4500 26-Port Software Version 3Com OS V3.03.02s168
Comware Platform Software, Software Version 3.10, Release 2202P13
3Com Switch 4500 26-Port uptime is 0 week, 0 day, 0 hour, 5 minutes"""

UNRECOGNIZED = "% Unrecognized command found at '^' position."


class FakeConsole:
    def __init__(self, version: str, prompt: str = "Quidway", responses: dict | None = None,
                 login: tuple[str, str] | None = None):
        self.version = version
        self.prompt = prompt
        self.responses = dict(responses or {})
        self.login = login
        self.state = "username" if login else "ready"
        self.buffer = bytearray()
        self.commands: list[str] = []
        self.opened = 0

    # pyserial-like surface used by the collectors
    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def reset_input_buffer(self):
        self.buffer.clear()

    def read(self, size: int) -> bytes:
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def _emit(self, text: str) -> None:
        self.buffer.extend(text.replace("\n", "\r\n").encode())

    def write(self, data: bytes) -> int:
        line = data.decode(errors="replace").rstrip("\r\n")
        if self.state == "username":
            if line:
                self._typed_user = line
                self.state = "password"
                self._emit("\nPassword:")
            else:
                self._emit("\nLogin authentication\n\nUsername:")
            return len(data)
        if self.state == "password":
            if (getattr(self, "_typed_user", ""), line) == self.login:
                self.state = "ready"
                self._emit(f"\n<{self.prompt}>")
            else:
                self.state = "username"
                self._emit("\n% Authentication failed\n\nUsername:")
            return len(data)
        if not line:
            self._emit(f"\n<{self.prompt}>")
            return len(data)
        self.commands.append(line)
        output = self.version if line == "display version" else self.responses.get(line, UNRECOGNIZED)
        self._emit(f"{line}\n{output}\n<{self.prompt}>")
        return len(data)

    def logout(self):
        """Simulate the switch's idle timeout on a login-protected console."""
        if self.login:
            self.state = "username"


def s3026c(**kwargs) -> FakeConsole:
    responses = {"display brief interface": S3026C_BRIEF, "display vlan": S3026C_VLANS,
                 "display interface Ethernet0/1": S3026C_PORT,
                 "display vlan 1": "VLAN ID: 1\nVLAN Type: static\nDescription: VLAN 0001\n"
                                   "Tagged   Ports: none\nUntagged Ports: Ethernet0/2",
                 "display vlan 10": "VLAN ID: 10\nVLAN Type: static\nDescription: uplink\n"
                                    "Tagged   Ports: none\nUntagged Ports: Ethernet0/1",
                 "display mac-address": "MAC ADDR        VLAN ID  STATE     PORT INDEX     AGING TIME(s)\n"
                                        "000f-e212-3456  10       Learned   Ethernet0/1    AGING"}
    responses.update(kwargs.pop("responses", {}))
    return FakeConsole(S3026C_VERSION, prompt=kwargs.pop("prompt", "S3026C"), responses=responses, **kwargs)
