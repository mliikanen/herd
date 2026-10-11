# Egress proxy for evaluation workers: CONNECT only, to allowed destinations only, TLS only, and logs each decision.
# ALLOW lists destinations as host or host:port (443 when no port is given), comma-separated; empty allows everything,
# which is only for measuring what a build reaches. A tunnel opens only if the client's first bytes are a TLS
# ClientHello whose server name (SNI) is the CONNECT host, so an allowed name can't carry other protocols or hosts.
import asyncio, os

ALLOW = set()
for entry in filter(None, os.environ.get("ALLOW", "").split(",")):
    ALLOW.add(entry if ":" in entry else f"{entry}:443")


def server_name(hello):
    """The SNI host name of a TLS ClientHello record, or None if it isn't one."""
    try:
        if hello[0] != 0x16 or hello[5] != 0x01:  # handshake record, ClientHello
            return None
        i = 5 + 4 + 2 + 32  # record header, handshake header, version, random
        i += 1 + hello[i]  # session id
        i += 2 + int.from_bytes(hello[i:i + 2], "big")  # cipher suites
        i += 1 + hello[i]  # compression methods
        end = i + 2 + int.from_bytes(hello[i:i + 2], "big")
        i += 2
        while i + 4 <= end:
            kind, size = int.from_bytes(hello[i:i + 2], "big"), int.from_bytes(hello[i + 2:i + 4], "big")
            if kind == 0:  # server_name: list length, then type 0 (host name), length, name
                name_length = int.from_bytes(hello[i + 7:i + 9], "big")
                return hello[i + 9:i + 9 + name_length].decode("ascii").lower()
            i += 4 + size
    except (IndexError, UnicodeDecodeError):
        pass
    return None


async def pipe(r, w):
    try:
        while (d := await r.read(65536)):
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()


async def handle(r, w):
    line = (await r.readline()).decode(errors="replace").split()
    while (await r.readline()) not in (b"\r\n", b"\n", b""):
        pass
    peer = w.get_extra_info("peername")[0]
    if len(line) < 2 or line[0] != "CONNECT" or ":" not in line[1]:
        print("REJECT-NONCONNECT", peer, " ".join(line[:2]), flush=True)
        w.write(b"HTTP/1.1 405 Only CONNECT\r\n\r\n")
        w.close()
        return
    target = line[1].lower()
    host, port = target.rsplit(":", 1)
    if ALLOW and target not in ALLOW:
        print("DENY", peer, target, flush=True)
        w.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        w.close()
        return
    w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await w.drain()
    try:  # the whole first TLS record: a ClientHello can span several reads
        header = await asyncio.wait_for(r.readexactly(5), timeout=30)
        body = await asyncio.wait_for(r.readexactly(int.from_bytes(header[3:5], "big")), timeout=30) \
            if header[0] == 0x16 else b""
        hello = header + body
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        hello = b""
    name = server_name(hello)
    if ALLOW and name != host:
        print("DENY-SNI", peer, target, name or "(not TLS)", flush=True)
        w.close()
        return
    try:
        ur, uw = await asyncio.open_connection(host, int(port))
    except Exception as e:
        print("FAIL", peer, target, e, flush=True)
        w.close()
        return
    print("ALLOW", peer, target, flush=True)
    uw.write(hello)
    await uw.drain()
    await asyncio.gather(pipe(r, uw), pipe(ur, w))


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 3128)
    async with server:
        await server.serve_forever()


asyncio.run(main())
