"""Stateful S3900 CLI simulator. No physical device or network is opened."""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()
VERSION = "Quidway 3900-SI 28-Port\nVRP (R) Software, Version 3.10(CN), RELEASE 0002\nS3900 VIRTUAL - simulated firmware response; not Huawei firmware"
UNRECOGNIZED = "% Unrecognized command found at '^' position."

# Simulated legacy VRP 3 / Comware 3 switches (QM_VIRTUAL_MODEL). The S3026C and
# 3Com behaviours are SYNTHETIC, based on the manuals, to exercise the adaptive
# configuration; they are not captures of real firmware.
MODELS = {
    "s3900": {
        "version": VERSION, "name": "S3900-Virtual",
        "ports": [f"Ethernet1/0/{n}" for n in range(1, 25)] + [f"GigabitEthernet1/1/{n}" for n in range(1, 5)],
        "brief": {"display interface brief", "display interface"}, "rejects": set(),
    },
    "s3026c": {
        "version": ("VRP (R) Software, Version 3.10, Release 0035\nQuidway S3026C uptime is 0 week, 0 day, 1 hour\n"
                    "S3026C VIRTUAL - simulated firmware response; not Huawei firmware"),
        "name": "S3026C-Virtual",
        "ports": [f"Ethernet0/{n}" for n in range(1, 25)] + ["GigabitEthernet1/1", "GigabitEthernet2/1"],
        "brief": {"display brief interface"}, "rejects": {"display interface brief"},
    },
    "3com4500": {
        "version": ("3Com Corporation\nComware Platform Software, Software Version 3.10, Release 2202P13\n"
                    "3Com Switch 4500 26-Port uptime is 0 week, 0 day, 0 hour\n"
                    "3COM 4500 VIRTUAL - simulated firmware response; not 3Com firmware"),
        "name": "4500-Virtual",
        "ports": [f"Ethernet1/0/{n}" for n in range(1, 25)] + ["GigabitEthernet1/0/25", "GigabitEthernet1/0/26"],
        # This model refuses the bulk hybrid cleanup: hybrid must move to the CLI.
        "brief": {"display brief interface"}, "rejects": {"display interface brief", "undo port hybrid vlan 1 to 4094"},
    },
}


def model_id():
    model = os.getenv("QM_VIRTUAL_MODEL", "s3900").strip().lower() or "s3900"
    if model not in MODELS:
        raise ValueError("QM_VIRTUAL_MODEL deve ser " + ", ".join(MODELS) + ".")
    return model


def model():
    return MODELS[model_id()]


def version():
    return model()["version"]


def port_names():
    return list(model()["ports"])


def state_path():
    settings = Path(os.getenv("QM_SETTINGS_FILE", Path(__file__).with_name("local-settings.ps1")))
    default = "s3900-virtual.json" if model_id() == "s3900" else f"s3900-virtual-{model_id()}.json"
    return Path(os.getenv("QM_VIRTUAL_STATE", settings.parent / "instance" / default)).expanduser()


def _initial():
    total = len(port_names())
    return {"name": model()["name"], "vlans": {"1": "default"},
            "ports": {str(n): {"admin": True, "link": n in {1, 4, 24, 25}, "mode": "access",
                               "pvid": 1, "tagged": [], "untagged": [1], "description": ""}
                      for n in range(1, total + 1)},
            "ips": {}, "routes": [], "last_saved": None, "saved": None}


def _load():
    path = state_path()
    if not path.exists():
        return _initial()
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or set(_initial()) - state.keys():
        raise ValueError("Estado virtual inválido; confira o arquivo de simulação.")
    return state


def _store(state):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        except Exception:
            f.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _interface(n):
    return port_names()[n - 1]


def _port_number(name):
    lowered = name.lower()
    for index, candidate in enumerate(port_names(), start=1):
        if candidate.lower() == lowered:
            return index
    return None


def _members(state, vlan):
    return ", ".join(f"{n} (tagged)" if vlan in p["tagged"] else n
                     for n,p in state["ports"].items() if vlan in p["tagged"] or vlan in p["untagged"]) or "—"


def snapshot():
    with _LOCK:
        s = _load()
        ports = [{"number": int(n), "interface": _interface(int(n)), "state": "up" if p["admin"] and p["link"] else "down",
                  "admin": p["admin"], "mode": p["mode"], "pvid": p["pvid"], "description": p["description"],
                  "tagged": p["tagged"], "untagged": p["untagged"]} for n,p in s["ports"].items()]
        lines = version().splitlines()
        label = next((line.split(" uptime")[0] for line in lines if " uptime is" in line), "Quidway 3900-SI 28-Port")
        release = next((line for line in lines if "Version" in line), "")
        return {"name": s["name"], "model": label + " · virtual", "version": release + " · simulado",
                "virtual_model": model_id(),
                "ports": ports, "vlans": [{"id": int(v), "name": name, "members": _members(s,int(v)), "note": "Virtual"} for v,name in sorted(s["vlans"].items(),key=lambda x:int(x[0]))],
                "ips": s["ips"], "routes": s["routes"], "last_saved": s["last_saved"],
                "dirty": s["saved"] != {k:v for k,v in s.items() if k not in {"saved","last_saved"}}}


def _configuration(s):
    lines = ["#", f"sysname {s['name']}"]
    for v,name in sorted(s["vlans"].items(),key=lambda x:int(x[0])):
        lines += ["#", f"vlan {v}", f" description {name}"]
    for n,p in s["ports"].items():
        lines += ["#", f"interface {_interface(int(n))}", f" port link-type {p['mode']}"]
        if p["mode"] == "access": lines += [f" port access vlan {p['pvid']}"]
        elif p["mode"] == "trunk":
            lines += [" port trunk permit vlan " + " ".join(map(str,sorted(set(p["tagged"]+p["untagged"])))), f" port trunk pvid vlan {p['pvid']}"]
        else:
            for tag in ("tagged","untagged"):
                if p[tag]: lines += [" port hybrid vlan " + " ".join(map(str,p[tag])) + " " + tag]
            lines += [f" port hybrid pvid vlan {p['pvid']}"]
        if p["description"]: lines += [" description " + p["description"]]
        if not p["admin"]: lines += [" shutdown"]
    for v,ip in s["ips"].items():
        lines += ["#", f"interface Vlan-interface {v}", f" ip address {ip['ip']} {ip['mask']}"]
    lines += ["#"] + ["ip route-static " + " ".join(r) for r in s["routes"]] + ["#", "return"]
    return "\n".join(lines)


class VirtualConsole:
    """Subset of pyserial's interface used by the existing collectors/executor."""
    def __init__(self):
        self.view = "user"
        self.target = None
        self.pending_save = False
        self.buffer = bytearray()
        self.closed = False
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def close(self): self.closed = True
    def reset_input_buffer(self): self.buffer.clear()
    def read(self,size):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data
    def _prompt(self,s):
        name=s["name"]
        if self.view=="user": return f"<{name}>"
        if self.view=="port":
            suffix=f"-{_interface(int(self.target or 1))}"
        else:
            suffix={"system":"","vlan":f"-vlan{self.target}","ip":f"-Vlan-interface{self.target}"}[self.view]
        return f"[{name}{suffix}]"
    def write(self,data):
        if self.closed: raise OSError("Console virtual encerrado.")
        command=data.decode("ascii").strip()
        with _LOCK:
            s=_load()
            try:
                output, changed = self._execute(s,command)
                if changed: _store(s)
            except (ValueError,KeyError,IndexError) as exc:
                output="Error: " + str(exc)
            self.buffer.extend((command + "\r\n" + (output+"\r\n" if output else "") +
                                ("[y/n]" if self.pending_save else self._prompt(s))).encode("utf-8"))
        return len(data)
    def _execute(self,s,c):
        if self.pending_save:
            self.pending_save=False
            if c.lower()=="y":
                s["saved"]=copy.deepcopy({k:v for k,v in s.items() if k not in {"saved","last_saved"}})
                s["last_saved"]=datetime.now(timezone.utc).isoformat()
                return "Configuration saved successfully (virtual).",True
            return "Save cancelled.",False
        if not c: return "",False
        if c in model()["rejects"]: return UNRECOGNIZED,False
        if c=="display version": return version(),False
        if c=="display vlan":
            return "Following VLAN exist(s):\n" + ", ".join(v + (" (default)" if v=="1" else "") for v in sorted(s["vlans"],key=int))+"\n\nVLAN ID  Name\n"+"\n".join(f"{v} {n}" for v,n in sorted(s["vlans"].items(),key=lambda x:int(x[0]))),False
        match = re.fullmatch(r"display vlan (\d+)", c)
        if match:
            vlan = str(int(match[1]))
            if vlan not in s["vlans"]:
                return "Error: The VLAN does not exist.", False
            tagged = [_interface(int(n)) for n,p in s["ports"].items() if int(vlan) in p["tagged"]]
            untagged = [_interface(int(n)) for n,p in s["ports"].items() if int(vlan) in p["untagged"]]
            return (f"VLAN ID: {vlan}\nDescription: {s['vlans'][vlan]}\nTagged Ports: " +
                    (" ".join(tagged) or "none") + "\nUntagged Ports: " + (" ".join(untagged) or "none")), False
        match = re.fullmatch(r"display interface ((?:GigabitEthernet|Ethernet)[0-9/]+)", c)
        if match:
            number = _port_number(match[1])
            if number is None:
                raise ValueError("Invalid interface")
            number = str(number)
            port = s["ports"][number]
            state = 'Administratively DOWN' if not port['admin'] else 'UP' if port['link'] else 'DOWN'
            return (f"{match[1]} current state : {state}\nDescription: {port['description']}\n"
                    f"PVID: {port['pvid']}\nPort link-type: {port['mode']}\nTagged VLAN ID: " +
                    (" ".join(map(str, port['tagged'])) or "none") + "\nUntagged VLAN ID: " +
                    (" ".join(map(str, port['untagged'])) or "none")), False
        if c in model()["brief"]:
            return "\n".join(f"{_interface(int(n))} {'UP' if p['admin'] and p['link'] else 'DOWN'} {p['mode']} PVID {p['pvid']}" for n,p in s["ports"].items()),False
        if c=="display current-configuration": return _configuration(s),False
        if c=="display saved-configuration": return _configuration(s["saved"]) if s["saved"] else "No saved configuration.",False
        if c=="display ip interface brief": return "\n".join(f"Vlan-interface{v} {p['ip']} {p['mask']}" for v,p in s["ips"].items()),False
        if c=="display ip routing-table": return "\n".join(" ".join(r) for r in s["routes"]) or "No static routes.",False
        if c=="return": self.view="user"; self.target=None; return "",False
        if c=="quit": self.view="system" if self.view in {"vlan","port","ip"} else "user"; self.target=None; return "",False
        if c=="system-view" and self.view=="user": self.view="system"; return "",False
        if c=="save" and self.view=="user": self.pending_save=True; return "Save current configuration?",False
        if self.view=="system":
            m=re.fullmatch(r"(undo )?vlan (\d+)",c)
            if m:
                v=str(int(m[2]))
                if not 1<=int(v)<=4094: raise ValueError("Invalid VLAN ID")
                if m[1]:
                    if v=="1": raise ValueError("Default VLAN protected")
                    if v not in s["vlans"]: raise ValueError("VLAN does not exist")
                    del s["vlans"][v]; s["ips"].pop(v,None)
                    for p in s["ports"].values():
                        for tag in ("tagged","untagged"): p[tag]=[x for x in p[tag] if x!=int(v)]
                        if p["pvid"]==int(v) and p["mode"] == "access":
                            p["pvid"]=1
                            if 1 not in p["untagged"]: p["untagged"].append(1)
                    return "",True
                s["vlans"].setdefault(v,f"VLAN{v}"); self.view="vlan"; self.target=v; return "",True
            m=re.fullmatch(r"sysname ([A-Za-z0-9_.-]{1,31})",c)
            if m: s["name"]=m[1]; return "",True
            m=re.fullmatch(r"interface ((?:GigabitEthernet|Ethernet)[0-9/]+)",c)
            if m:
                n=_port_number(m[1])
                if n is None: raise ValueError("Invalid interface")
                self.view="port"; self.target=str(n); return "",False
            m=re.fullmatch(r"interface Vlan-interface (\d+)",c)
            if m:
                if str(int(m[1])) not in s["vlans"]: raise ValueError("VLAN does not exist")
                self.view="ip"; self.target=str(int(m[1])); return "",False
            m=re.fullmatch(r"(undo )?ip route-static (\S+) (\S+) (\S+)",c)
            if m:
                net=ipaddress.IPv4Network(f"{m[2]}/{m[3]}",strict=False)
                hop=ipaddress.IPv4Address(m[4])
                if hop.is_multicast or hop.is_unspecified or str(hop)=="255.255.255.255": raise ValueError("Invalid next hop")
                r=[str(net.network_address),str(net.netmask),str(hop)]
                if m[1]:
                    if r not in s["routes"]: raise ValueError("Route not found")
                    s["routes"].remove(r)
                elif r not in s["routes"]: s["routes"].append(r)
                return "",True
        if self.view=="vlan":
            m=re.fullmatch(r"description ([A-Za-z0-9_.-]{1,31})",c)
            if m: s["vlans"][self.target]=m[1]; return "",True
        if self.view=="ip":
            m=re.fullmatch(r"ip address (\S+) (\S+)",c)
            if m:
                ip=ipaddress.IPv4Interface(f"{m[1]}/{m[2]}")
                if not 1<=ip.network.prefixlen<=30 or ip.ip in {ip.network.network_address,ip.network.broadcast_address} or ip.ip.is_multicast or ip.ip.is_unspecified: raise ValueError("Invalid management address")
                s["ips"][self.target]={"ip":str(ip.ip),"mask":str(ip.netmask)}; return "",True
        if self.view=="port":
            p=s["ports"][self.target]
            m=re.fullmatch(r"port link-type (access|trunk|hybrid)",c)
            if m:
                if p["mode"] != m[1] and p["mode"] != "access" and m[1] != "access":
                    raise ValueError("A trunk/hybrid transition must pass through access")
                if p["mode"]!=m[1]: p.update(mode=m[1],pvid=1,tagged=[],untagged=[1])
                return "",True
            if c in {"shutdown","undo shutdown"}: p["admin"]=c!="shutdown"; return "",True
            m=re.fullmatch(r"description ([A-Za-z0-9 _.-]{1,64})",c)
            if m: p["description"]=m[1]; return "",True
            m=re.fullmatch(r"port (access vlan|trunk pvid vlan|hybrid pvid vlan) (\d+)",c)
            if m:
                v=int(m[2]); kind=m[1].split()[0]
                if p["mode"]!=kind or str(v) not in s["vlans"]: raise ValueError("Invalid port mode or VLAN")
                if kind=="access": p.update(pvid=v,tagged=[],untagged=[v])
                elif kind=="trunk":
                    allowed=set(p["tagged"]+p["untagged"])
                    if v not in allowed: raise ValueError("PVID not permitted")
                    p.update(pvid=v,untagged=[v],tagged=sorted(allowed-{v}))
                else:
                    if v not in p["untagged"]: raise ValueError("PVID must be untagged")
                    p["pvid"]=v
                return "",True
            if c == "undo port trunk permit vlan all" and p["mode"] == "trunk":
                p.update(tagged=[], untagged=[])
                return "", True
            if c == "undo port hybrid vlan 1 to 4094" and p["mode"] == "hybrid":
                p.update(tagged=[], untagged=[])
                return "", True
            m=re.fullmatch(r"port trunk permit vlan ([0-9 ]+)",c)
            if m and p["mode"]=="trunk":
                ids=set(map(int,m[1].split()))
                if any(str(v) not in s["vlans"] for v in ids): raise ValueError("VLAN does not exist")
                allowed=set(p["tagged"]+p["untagged"])|ids
                p["tagged"]=sorted(allowed-{p["pvid"]}); p["untagged"]=sorted(allowed & {p["pvid"]}); return "",True
            m=re.fullmatch(r"port hybrid vlan ([0-9 ]+) (tagged|untagged)",c)
            if m and p["mode"]=="hybrid":
                ids=set(map(int,m[1].split()))
                if any(str(v) not in s["vlans"] for v in ids): raise ValueError("VLAN does not exist")
                other="untagged" if m[2]=="tagged" else "tagged"
                p[m[2]]=sorted(set(p[m[2]])|ids); p[other]=sorted(set(p[other])-ids); return "",True
        raise ValueError("Unrecognized command or invalid view (virtual)")
