#!/usr/bin/env python3
"""启动冷库调拨核对服务。用法: python3 run_server.py [host] [port] [db_path]"""
import sys

from coldchain.api import serve

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    db = sys.argv[3] if len(sys.argv) > 3 else "coldchain.db"
    serve(db, host, port)
