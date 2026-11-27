#!/usr/bin/env python3
"""SteerSight build tool.

Bundles src/css/*.css and src/js/*.js into the HTML template at
src/index.html and writes ONE self-contained file (default:
dist/steersight.html). Python standard library only.

Usage
-----
    python build.py                 build dist/steersight.html
    python build.py --min           also minify the CSS
    python build.py --check         build, then syntax-check the JS with Node
    python build.py --serve         build, then serve dist/ on http://localhost:8000
    python build.py --watch         rebuild whenever a file in src/ changes
    python build.py --watch --serve development loop (rebuild + local server)
    python build.py --out dist/index.html     choose the output file
    python build.py --clean         delete dist/

Rules
-----
* Files are bundled in filename order, so keep the numeric prefixes
  (00-, 10-, ...) when you add a module.
* The template must contain the placeholders:
      <!-- build:banner -->   version / build-time comment
      <!-- build:css -->      all CSS, wrapped in <style>
      <!-- build:js -->       all JS, wrapped in <script>
* The JS files share one global scope (classic scripts, not ES modules).
"""
import argparse
import datetime
import functools
import http.server
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
VERSION = "2.0.0"
PLACEHOLDERS = ("<!-- build:banner -->", "<!-- build:css -->", "<!-- build:js -->")


def read_sorted(folder: pathlib.Path, pattern: str) -> list[tuple[str, str]]:
    files = sorted(folder.glob(pattern))
    if not files:
        sys.exit(f"error: no files matching {pattern} in {folder}")
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


def minify_css(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)       # comments
    css = re.sub(r"\s+", " ", css)                         # whitespace runs
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css)           # around punctuation
    css = re.sub(r";}", "}", css)                          # last semicolon
    return css.strip()


def bundle_css(minify: bool) -> str:
    parts = [text for _, text in read_sorted(SRC / "css", "*.css")]
    css = "\n".join(parts)
    return minify_css(css) if minify else css


def bundle_js() -> str:
    js = "\n".join(text.rstrip("\n") for _, text in read_sorted(SRC / "js", "*.js"))
    if "</script" in js.lower():
        sys.exit("error: JS contains '</script', which would end the inline script early")
    return js


def build(out: pathlib.Path, minify: bool = False) -> pathlib.Path:
    template = (SRC / "index.html").read_text(encoding="utf-8")
    for token in PLACEHOLDERS:
        if template.count(token) != 1:
            sys.exit(f"error: src/index.html must contain {token} exactly once")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = (
        template.replace(PLACEHOLDERS[0], f"<!-- SteerSight v{VERSION}, built {stamp} -->")
        .replace(PLACEHOLDERS[1], "<style>\n" + bundle_css(minify) + "\n</style>")
        .replace(PLACEHOLDERS[2], "<script>\n" + bundle_js() + "\n</script>")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"built {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}  ({len(html) / 1024:.1f} KB)")
    return out


def check_js() -> None:
    node = shutil.which("node")
    if not node:
        print("skip --check: Node.js not found")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(bundle_js())
    result = subprocess.run([node, "--check", tmp.name], capture_output=True, text=True)
    pathlib.Path(tmp.name).unlink(missing_ok=True)
    if result.returncode:
        sys.exit("JS syntax error:\n" + result.stderr)
    print("JS syntax OK")


def snapshot() -> dict[str, float]:
    return {str(p): p.stat().st_mtime for p in SRC.rglob("*") if p.is_file()}


def serve(folder: pathlib.Path, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    print(f"serving {folder.name}/ at http://localhost:{port}/  (Ctrl+C to stop)")
    httpd.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build SteerSight into one HTML file.")
    ap.add_argument("--out", default=str(DIST / "steersight.html"), help="output file")
    ap.add_argument("--min", action="store_true", help="minify CSS")
    ap.add_argument("--check", action="store_true", help="syntax-check the bundled JS with Node")
    ap.add_argument("--serve", action="store_true", help="serve the output folder on localhost")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--watch", action="store_true", help="rebuild when src/ changes")
    ap.add_argument("--clean", action="store_true", help="delete dist/")
    args = ap.parse_args()

    if args.clean:
        shutil.rmtree(DIST, ignore_errors=True)
        print("removed dist/")
        return

    out = pathlib.Path(args.out).resolve()
    build(out, args.min)
    if args.check:
        check_js()

    if args.serve:
        threading.Thread(target=serve, args=(out.parent, args.port), daemon=True).start()
    if args.watch or args.serve:
        seen = snapshot()
        try:
            while True:
                time.sleep(0.5)
                if args.watch and (now := snapshot()) != seen:
                    seen = now
                    build(out, args.min)
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
