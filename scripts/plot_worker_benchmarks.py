"""Render measured benchmark JSON as a dependency-free SVG line chart."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "benchmarks" / "worker" / "raw"
files = sorted(root.rglob("workers-*-depth-*.json"))
data = [json.loads(path.read_text(encoding="utf-8")) for path in files]
data = [item for item in data if item.get("status") == "measured"]
if not data:
    print("No measured benchmark JSON found; no plot created.", file=sys.stderr)
    raise SystemExit(2)
depths = sorted({int(item["queue_depth"]) for item in data})
workers = sorted({int(item["worker_processes"]) for item in data})
labels = sorted({path.parent.name for path in files})
w, h, left, top, plotw, ploth = 900, 520, 80, 50, 760, 380
maximum = max(float(item["throughput_jobs_per_second"]) for item in data) or 1
colors = ["#38bdf8", "#34d399", "#fbbf24", "#fb7185", "#a78bfa", "#f472b6"]
parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
         '<rect width="100%" height="100%" fill="#0b1220"/>',
         '<text x="450" y="26" text-anchor="middle" fill="white" font-family="sans-serif" font-size="18">Chronos measured throughput</text>']
for tick in range(6):
    y = top + ploth - tick * ploth / 5
    value = maximum * tick / 5
    parts += [f'<line x1="{left}" x2="{left+plotw}" y1="{y:.1f}" y2="{y:.1f}" stroke="#334155"/>',
              f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" fill="#cbd5e1" font-family="sans-serif" font-size="11">{value:.1f}</text>']
for i, depth in enumerate(depths):
    x = left + (plotw * i / max(1, len(depths)-1))
    parts.append(f'<text x="{x:.1f}" y="{top+ploth+24}" text-anchor="middle" fill="#cbd5e1" font-family="sans-serif" font-size="12">queue {depth}</text>')
series = [(label,count) for label in labels for count in workers]
for index, (label,count) in enumerate(series):
    points=[]
    for i, depth in enumerate(depths):
        found=next((item for path,item in zip(files,data) if path.parent.name==label and int(item["worker_processes"])==count and int(item["queue_depth"])==depth),None)
        if found:
            x=left+(plotw*i/max(1,len(depths)-1));y=top+ploth-(float(found["throughput_jobs_per_second"])/maximum)*ploth;points.append((x,y))
    if points:
        col=colors[index%len(colors)]
        parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="3" points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in points)}"/>')
        for x,y in points: parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{col}"/>')
        parts.append(f'<text x="{left+(index%6)*130}" y="{h-22-(index//6)*18}" fill="{col}" font-family="sans-serif" font-size="12">{label} workers={count}</text>')
parts.append('<text transform="translate(20 250) rotate(-90)" text-anchor="middle" fill="#cbd5e1" font-family="sans-serif" font-size="12">jobs / second</text>')
parts.append('</svg>')
target=root.parent/"throughput.svg"
target.write_text("\n".join(parts)+"\n",encoding="utf-8")
print(target)
