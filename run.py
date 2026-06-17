"""Entry point: python run.py [--host H] [--port P] [--reload]"""
import argparse

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="cc_mgr — local Claude project viewer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    print(f"cc_mgr serving on http://{args.host}:{args.port}")
    uvicorn.run("backend.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
