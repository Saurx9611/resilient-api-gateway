"""Bounded loopback-only direct/proxy/429/circuit benchmark. See docs/benchmarks.md."""
import argparse, concurrent.futures, http.client, json, math, os, platform, random, socket, statistics, subprocess, threading, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
local = threading.local()
class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(500 if getattr(self.server, "fail", False) else 200)
        self.send_header("Content-Length", "1024")
        self.end_headers(); self.wfile.write(b"x" * 1024)
    def log_message(self, *_): pass

def client_call(url):
    p = urllib.parse.urlsplit(url); key = (p.hostname, p.port)
    if getattr(local, "key", None) != key:
        local.conn = http.client.HTTPConnection(*key, timeout=4); local.key = key
    start = time.perf_counter_ns()
    try:
        local.conn.request("GET", p.path)
        response = local.conn.getresponse(); response.read()
        return response.status, (time.perf_counter_ns()-start)/1e6
    except Exception:
        try: local.conn.close()
        except Exception: pass
        local.key = None
        return 0, (time.perf_counter_ns()-start)/1e6

def measure(name, url, count, concurrency, expected):
    samples=[]; statuses={}; lock=threading.Lock()
    def one(_):
        status, ms=client_call(url)
        with lock: samples.append(ms); statuses[str(status)]=statuses.get(str(status),0)+1
    started=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool: list(pool.map(one, range(count)))
    duration=time.perf_counter()-started; ordered=sorted(samples)
    # Nearest-rank: rank=ceil(q*n), converted from one-based to zero-based.
    pct=lambda q: round(ordered[min(len(ordered)-1, max(0, math.ceil(q*len(ordered))-1))], 3)
    errors=count-statuses.get(str(expected),0)
    return dict(name=name, requests=count, concurrency=concurrency, duration_seconds=round(duration,6), requests_per_second=round(count/duration,3), statuses=statuses, errors=errors, error_percent=round(errors*100/count,4), p50_ms=pct(.50), p95_ms=pct(.95), p99_ms=pct(.99))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--requests",type=int,default=300); ap.add_argument("--concurrency",type=int,default=8); ap.add_argument("--redis-url",default=os.getenv("REDIS_TEST_URL","redis://127.0.0.1:6379/0")); ap.add_argument("--output",default="")
    a=ap.parse_args()
    if not 1 <= a.requests <= 10000 or not 1 <= a.concurrency <= 64: raise SystemExit("requests 1..10000; concurrency 1..64")
    rp=urllib.parse.urlsplit(a.redis_url)
    try:
        if rp.scheme not in ("redis","rediss") or not socket.getaddrinfo(rp.hostname, rp.port or 6379)[0][4][0].startswith(("127.","::1")): raise ValueError()
    except Exception: raise SystemExit("Redis must resolve to a loopback address")
    server=ThreadingHTTPServer(("127.0.0.1",0),Upstream); threading.Thread(target=server.serve_forever,daemon=True).start()
    server2=ThreadingHTTPServer(("127.0.0.1",0),Upstream); threading.Thread(target=server2.serve_forever,daemon=True).start()
    upstream=f"http://127.0.0.1:{server.server_port}"
    upstream2=f"http://127.0.0.1:{server2.server_port}"
    binpath=ROOT/"bin"/("gateway.exe" if os.name=="nt" else "gateway")
    if not binpath.exists(): raise SystemExit("build first: go build -o bin/ ./cmd/...")
    with socket.socket() as sock: sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
    gateway_url=f"http://127.0.0.1:{port}"; env=os.environ.copy(); env["REDIS_URL"]=a.redis_url
    args=[str(binpath),"-listen",f"127.0.0.1:{port}","-upstream-one",upstream,"-upstream-two",upstream2,"-rate-namespace","bench"+uuid.uuid4().hex[:12],"-limit-one","1","-limit-two","100000","-circuit-failures","10000","-circuit-open","30s"]
    proc=subprocess.Popen(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    try:
        for _ in range(100):
            if proc.poll() is not None: raise RuntimeError("gateway exited; check Redis connectivity")
            try:
                c=http.client.HTTPConnection("127.0.0.1",port,timeout=.1); c.request("GET","/readyz"); ready=c.getresponse(); ready.read(); c.close()
                if ready.status == 200: break
            except Exception: time.sleep(.05)
            time.sleep(.05)
        else: raise RuntimeError("gateway startup timeout")
        # One measured warmup exercises the connection pool; excluded from counts.
        client_call(upstream+"/warmup")
        client_call(gateway_url+"/two/warmup")
        direct=measure("direct", upstream+"/payload",a.requests,a.concurrency,200)
        proxied=measure("proxied",gateway_url+"/two/payload",a.requests,a.concurrency,200)
        client_call(gateway_url+"/one/prime")
        limited=measure("429_fail_fast",gateway_url+"/one/payload",min(a.requests,40),a.concurrency,429)
        proc.terminate(); proc.wait(timeout=5)
        server2.fail=True
        with socket.socket() as sock: sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
        gateway_url=f"http://127.0.0.1:{port}"
        args=[str(binpath),"-listen",f"127.0.0.1:{port}","-upstream-one",upstream,"-upstream-two",upstream2,"-rate-namespace","bench"+uuid.uuid4().hex[:12],"-limit-one","100000","-limit-two","100000","-circuit-failures","1","-circuit-open","30s"]
        proc=subprocess.Popen(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        for _ in range(100):
            if proc.poll() is not None: raise RuntimeError("circuit gateway exited")
            try:
                c=http.client.HTTPConnection("127.0.0.1",port,timeout=.1); c.request("GET","/readyz"); ready=c.getresponse(); ready.read(); c.close()
                if ready.status == 200: break
            except Exception: time.sleep(.05)
            time.sleep(.05)
        else: raise RuntimeError("circuit gateway startup timeout")
        # First request trips threshold=1; following requests are circuit-open 503.
        client_call(gateway_url+"/two/trip")
        opened=measure("circuit_open_fail_fast",gateway_url+"/two/payload",min(a.requests,40),a.concurrency,503)
        report={"schema":"rag-bench-v1","utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"environment":{"os":platform.platform(),"machine":platform.machine(),"processor":platform.processor(),"logical_cpu_count":os.cpu_count(),"go_version":subprocess.check_output([str(ROOT/".tools/go/bin/go.exe" if os.name=="nt" else "go"),"version"],text=True).strip(),"payload_bytes":1024,"http":"HTTP/1.1 cleartext loopback keep-alive","redis":"loopback; admission enabled","warmup":"one unmeasured direct and proxied request; first startup excluded","test_duration":"per scenario recorded","concurrency":a.concurrency,"requested_requests":a.requests,"limitations":"single host Python ThreadingHTTPServer fixture; Python client and upstream may dominate; no TLS; sample percentiles use nearest-rank; no production inference"},"scenarios":[direct,proxied,limited,opened]}
        raw=json.dumps(report,indent=2)+"\n"
        if a.output: Path(a.output).write_text(raw,encoding="utf-8")
        print(raw,end="")
    finally:
        proc.terminate(); proc.wait(timeout=5); server.shutdown(); server.server_close(); server2.shutdown(); server2.server_close()

if __name__=="__main__": main()
