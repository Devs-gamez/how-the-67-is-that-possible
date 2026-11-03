"""Voice/video calls: WebSocket signalling for peer-to-peer WebRTC.

Audio and video go directly between browsers; this server only relays the small setup messages
(offers, answers, ICE candidates), keeps track of rooms, and rings people.

* Rooms: anyone can join a named public room (default "lobby"), up to MAX_ROOM people.
* Direct calls: ring someone who has switched on "Available for calls" (OFF by default). The two of you
  get a private room that nobody else can join.
"""
import asyncio
import os
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict, deque

from chat import ROOM as CHAT_ROOM, clean_nick, same_site

MAX_ROOM = 8                   # mesh calls get heavy past this
MAX_PER_IP = 6
MAX_TOTAL = 300
MAX_FRAME = 32768              # SDP offers can be several KB
RING_SECONDS = int(os.environ.get("RING_SECONDS", "30"))
RATE_MSGS, RATE_WINDOW = 400, 10        # all messages (ICE candidates arrive in bursts)
CALLS_PER_MIN = 4
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,23}$")


class Client:
    def __init__(self, send, ip):
        self.send, self.ip = send, ip
        self.id = secrets.token_hex(4)
        self.nick = clean_nick("")
        self.tag = hashlib.sha256((CHAT_ROOM.salt + ip).encode()).hexdigest()[:4]   # same tag as in chat
        self.available = False
        self.room = None
        self.mic, self.cam = True, False
        self.ringing_in = None      # private room name we're being rung for
        self.ringing_out = None     # private room name we rang
        self.times = deque()
        self.call_times = deque()
        self.lock = asyncio.Lock()
        self.dead = False

    def meta(self):
        return {"id": self.id, "nick": self.nick, "tag": self.tag, "mic": self.mic, "cam": self.cam}

    async def push(self, obj):
        if self.dead:
            return
        try:
            async with self.lock:
                await asyncio.wait_for(
                    self.send({"type": "websocket.send", "text": json.dumps(obj, separators=(",", ":"))}), 5)
        except Exception:
            self.dead = True


class Invite:
    def __init__(self, caller, callee):
        self.caller, self.callee = caller, callee   # client ids
        self.answered = False
        self.handle = None


class Hub:
    def __init__(self):
        self.clients = {}
        self.per_ip = defaultdict(int)
        self.rooms = {}          # name -> [Client]
        self.invites = {}        # private room name -> Invite
        self._lists_pending = False

    # ---------------------------------------------------------------- lists
    def schedule_lists(self):
        if not self._lists_pending:
            self._lists_pending = True
            asyncio.get_running_loop().call_later(0.25, lambda: asyncio.ensure_future(self.send_lists()))

    async def send_lists(self):
        self._lists_pending = False
        people = [{"id": c.id, "nick": c.nick, "tag": c.tag, "busy": bool(c.room or c.ringing_in)}
                  for c in self.clients.values() if c.available]
        rooms = [{"name": n, "count": len(m)} for n, m in self.rooms.items() if not n.startswith("~") and m]
        await asyncio.gather(*(c.push({"t": "lists", "people": people if c.available else [], "rooms": rooms})
                               for c in list(self.clients.values())), return_exceptions=True)

    async def to_room(self, name, obj, skip=None):
        await asyncio.gather(*(m.push(obj) for m in list(self.rooms.get(name, [])) if m is not skip),
                             return_exceptions=True)

    # ---------------------------------------------------------------- rooms
    async def join(self, c, name):
        await self.leave(c)
        inv = None
        if isinstance(name, str) and name.startswith("~"):
            inv = self.invites.get(name)
            if not inv or c.id not in (inv.caller, inv.callee):
                await c.push({"t": "error", "text": "That call is no longer available."})
                return
        else:
            name = re.sub(r"\s+", " ", str(name or "lobby")).strip().lower() or "lobby"
            if not ROOM_RE.match(name):
                await c.push({"t": "error", "text": "Room names use letters, numbers, spaces, - and _ (max 24)."})
                return
        members = self.rooms.setdefault(name, [])
        if len(members) >= MAX_ROOM:
            await c.push({"t": "error", "text": "That room is full (%d people max)." % MAX_ROOM})
            return
        peers = [m.meta() for m in members]
        members.append(c)
        c.room, c.mic, c.cam = name, True, False
        if inv and c.id == inv.callee:
            inv.answered = True
            if inv.handle:
                inv.handle.cancel()
            c.ringing_in = None
            caller = self.clients.get(inv.caller)
            if caller:
                caller.ringing_out = None      # call connected: the caller is free to ring again later
        await c.push({"t": "joined", "room": name, "you": c.meta(), "peers": peers})
        await self.to_room(name, {"t": "peer-join", "peer": c.meta()}, skip=c)
        self.schedule_lists()

    async def leave(self, c):
        name = c.room
        if not name:
            return
        c.room = None
        members = self.rooms.get(name, [])
        if c in members:
            members.remove(c)
        inv = self.invites.get(name)
        if inv and not inv.answered and c.id == inv.caller:
            await self.end_invite(name, "cancelled")     # caller hung up before anyone answered
        if not members:
            self.rooms.pop(name, None)
            self.invites.pop(name, None)
        else:
            await self.to_room(name, {"t": "peer-leave", "id": c.id})
        self.schedule_lists()

    # ---------------------------------------------------------------- ringing
    async def end_invite(self, name, outcome, who=None):
        """outcome: cancelled (caller hung up) | declined | missed (nobody answered)"""
        inv = self.invites.pop(name, None)
        if not inv:
            return
        if inv.handle:
            inv.handle.cancel()
        caller, callee = self.clients.get(inv.caller), self.clients.get(inv.callee)
        if callee:
            callee.ringing_in = None
        if caller:
            caller.ringing_out = None
        if outcome == "cancelled" and callee:
            await callee.push({"t": "cancelled", "room": name})
        elif outcome == "declined" and caller:
            await caller.push({"t": "declined", "room": name, "nick": who or "They"})
        elif outcome == "missed":
            for x in (caller, callee):
                if x:
                    await x.push({"t": "missed", "room": name})
        self.schedule_lists()

    async def call(self, c, to_id):
        now = time.monotonic()
        while c.call_times and now - c.call_times[0] > 60:
            c.call_times.popleft()
        if len(c.call_times) >= CALLS_PER_MIN:
            await c.push({"t": "error", "text": "You're calling too often. Wait a minute."})
            return
        callee = self.clients.get(to_id)
        if not callee or callee is c or not callee.available:
            await c.push({"t": "error", "text": "That person isn't available."})
            return
        if c.room or c.ringing_out:
            await c.push({"t": "error", "text": "Leave your current call first."})
            return
        if callee.room or callee.ringing_in:
            await c.push({"t": "busy", "nick": callee.nick})
            return
        c.call_times.append(now)
        room = "~" + secrets.token_hex(5)
        inv = Invite(c.id, callee.id)
        self.invites[room] = inv
        c.ringing_out, callee.ringing_in = room, room
        inv.handle = asyncio.get_running_loop().call_later(
            RING_SECONDS, lambda: asyncio.ensure_future(self.end_invite(room, "missed")))
        await callee.push({"t": "incoming", "from": c.meta(), "room": room, "seconds": RING_SECONDS})
        await c.push({"t": "ringing", "to": callee.meta(), "room": room})
        self.schedule_lists()

    async def respond(self, c, room, accept):
        inv = self.invites.get(room)
        if not inv or inv.callee != c.id or inv.answered:
            return
        if not accept:
            await self.end_invite(room, "declined", who=c.nick)

    async def cancel(self, c, room):
        inv = self.invites.get(room)
        if inv and inv.caller == c.id and not inv.answered:
            await self.end_invite(room, "cancelled")

    # ---------------------------------------------------------------- relay
    async def signal(self, c, to_id, data):
        t = self.clients.get(to_id)
        if not t or not c.room or t.room != c.room or not isinstance(data, dict):
            return
        if len(json.dumps(data)) > 24000:
            return
        await t.push({"t": "signal", "from": c.id, "data": data})

    async def drop(self, c):
        c.dead = True
        ro, ri = c.ringing_out, c.ringing_in
        await self.leave(c)
        if ro:
            await self.end_invite(ro, "cancelled")
        if ri:
            await self.end_invite(ri, "declined", who=c.nick)
        self.clients.pop(c.id, None)
        self.per_ip[c.ip] -= 1
        if self.per_ip[c.ip] <= 0:
            self.per_ip.pop(c.ip, None)
        self.schedule_lists()


HUB = Hub()


async def calls_asgi(scope, receive, send):
    msg = await receive()
    if msg["type"] != "websocket.connect":
        return
    ip = (scope.get("client") or ("?", 0))[0]
    if not same_site(scope):
        await send({"type": "websocket.close", "code": 1008})
        return
    if HUB.per_ip[ip] >= MAX_PER_IP or len(HUB.clients) >= MAX_TOTAL:
        await send({"type": "websocket.close", "code": 1013})
        return
    await send({"type": "websocket.accept"})
    c = Client(send, ip)
    HUB.clients[c.id] = c
    HUB.per_ip[ip] += 1
    try:
        await c.push({"t": "you", **c.meta()})
        HUB.schedule_lists()
        while True:
            m = await receive()
            if m["type"] == "websocket.disconnect":
                break
            raw = m.get("text")
            if raw is None:
                raw = (m.get("bytes") or b"").decode("utf-8", "ignore")
            if len(raw) > MAX_FRAME:
                continue
            now = time.monotonic()
            while c.times and now - c.times[0] > RATE_WINDOW:
                c.times.popleft()
            if len(c.times) >= RATE_MSGS:
                continue
            c.times.append(now)
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            k = d.get("t")
            if k == "hello":
                c.nick = clean_nick(d.get("nick", ""))
                await c.push({"t": "you", **c.meta()})
                HUB.schedule_lists()
                if c.room:
                    await HUB.to_room(c.room, {"t": "peer-info", "peer": c.meta()}, skip=c)
            elif k == "avail":
                c.available = bool(d.get("on"))
                HUB.schedule_lists()
            elif k == "join":
                await HUB.join(c, d.get("room"))
            elif k == "leave":
                await HUB.leave(c)
            elif k == "signal":
                await HUB.signal(c, d.get("to"), d.get("data"))
            elif k == "state" and c.room:
                c.mic, c.cam = bool(d.get("mic")), bool(d.get("cam"))
                await HUB.to_room(c.room, {"t": "peer-state", "id": c.id, "mic": c.mic, "cam": c.cam}, skip=c)
            elif k == "call":
                await HUB.call(c, d.get("to"))
            elif k == "respond":
                await HUB.respond(c, d.get("room"), bool(d.get("accept")))
            elif k == "cancel":
                await HUB.cancel(c, d.get("room"))
    finally:
        await HUB.drop(c)
