# Logging CONNECT proxy: allows everything (or only ALLOW, if set) and logs each destination.
import asyncio, os, sys
ALLOW = set(filter(None, os.environ.get("ALLOW", "").split(",")))
async def pipe(r, w):
    try:
        while (d := await r.read(65536)): w.write(d); await w.drain()
    except Exception: pass
    finally: w.close()
async def handle(r, w):
    line = (await r.readline()).decode(errors="replace").split()
    while (await r.readline()) not in (b"\r\n", b"\n", b""): pass
    if len(line) < 2 or line[0] != "CONNECT":
        print("REJECT-NONCONNECT", " ".join(line[:2]), flush=True); w.write(b"HTTP/1.1 405 Only CONNECT\r\n\r\n"); w.close(); return
    host, port = line[1].rsplit(":", 1)
    ok = not ALLOW or host in ALLOW
    print(("ALLOW " if ok else "DENY ") + line[1], flush=True)
    if not ok: w.write(b"HTTP/1.1 403 Forbidden\r\n\r\n"); w.close(); return
    try: ur, uw = await asyncio.open_connection(host, int(port))
    except Exception as e: print("FAIL", line[1], e, flush=True); w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n"); w.close(); return
    w.write(b"HTTP/1.1 200 Connection established\r\n\r\n"); await w.drain()
    await asyncio.gather(pipe(r, uw), pipe(ur, w))
async def main():
    s = await asyncio.start_server(handle, "0.0.0.0", 3128)
    async with s: await s.serve_forever()
asyncio.run(main())
