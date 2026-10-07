# -*- coding: utf-8 -*-
"""
蓝电协议【模拟服务器】——用于在没有真机/蓝电软件的情况下测试本上位机。
实现 3 个接口：/OpearMon/control, /QueryChlStatus/control, /QueryChlRP/control
运行后，在上位机里把 主机IP 填 127.0.0.1、端口填 7777 即可联调。

用法:  python landt_mock_server.py   (默认端口 7777，可用 --port 指定)
"""
import json
import time
import random
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHANNEL_COUNT = 8
BOX = 1

# 通道内部状态： nStatus 0/1/2/3/4 = 测试中/停止/完成/故障/无状态
_state = {chl: {"nStatus": 1, "batt": "", "start": None, "cap": 0.0, "loop": 0}
          for chl in range(1, CHANNEL_COUNT + 1)}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 关闭默认访问日志

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        req = self._read_json()

        if path == "/OpearMon/control":
            return self._handle_control(req)
        if path == "/QueryChlStatus/control":
            return self._handle_status()
        if path == "/QueryChlRP/control":
            return self._handle_rp(req)

        self._send({"nCode": -404, "strMsg": "未知接口: %s" % path})

    def _handle_control(self, req):
        opera = int(req.get("nOperaType", 0))
        chls = req.get("MultipleChls", []) or []
        names = {0: "启动", 1: "续接", 2: "停止"}
        done = []
        for c in chls:
            chl = int(c.get("nChl", 0))
            if chl not in _state:
                continue
            if opera == 0:      # 启动
                _state[chl].update(nStatus=0, batt=c.get("strBattNo", ""),
                                   start=_now(), cap=0.0, loop=0)
            elif opera == 1:    # 续接
                _state[chl]["nStatus"] = 0
                if not _state[chl]["start"]:
                    _state[chl]["start"] = _now()
            elif opera == 2:    # 停止
                _state[chl]["nStatus"] = 1
            done.append("%03d_%d" % (BOX, chl))
        print("[模拟器] %s 通道: %s  (条码=%s)" % (
            names.get(opera, opera), ",".join(done),
            ",".join(str(c.get("strBattNo", "")) for c in chls)))
        self._send({"nCode": 0, "strMsg": "%s成功: %s" % (names.get(opera, opera), ",".join(done))})

    def _handle_status(self):
        arr = [{"nBox": BOX, "nChl": chl, "nStatus": st["nStatus"]}
               for chl, st in _state.items()]
        self._send({"nCode": 0, "strMsg": "ok", "AllChlStatus": arr})

    def _handle_rp(self, req):
        want = req.get("vecAFO", []) or []
        want_chls = set(int(x.get("nChl")) for x in want) if want else set(_state.keys())
        arr = []
        for chl, st in _state.items():
            if chl not in want_chls:
                continue
            testing = st["nStatus"] == 0
            if testing:          # 运行中模拟数据缓慢变化
                st["cap"] += random.uniform(0.0, 0.05)
            volt = round(random.uniform(3.0, 4.2), 4) if testing else 0.0
            curr = round(random.uniform(50, 1000), 3) if testing else 0.0
            arr.append({
                "Cap": round(st["cap"], 4),
                "Loop": st["loop"],
                "Curr": curr,
                "Egy": round(st["cap"] * volt, 4),
                "CexName": (st["batt"] + "_%03d_%d" % (BOX, chl)) if st["batt"] else "",
                "Chl": "%03d_%d" % (BOX, chl),
                "Status": st["nStatus"],
                "StepNo": 1 if testing else 0,
                "StepSpan": "00:01:%02d" % random.randint(0, 59) if testing else "00:00:00",
                "Temperature": round(random.uniform(24, 30), 1),
                "StartTime": st["start"] or "",
                "Volt": volt,
            })
        self._send({"nCode": 0, "strMsg": "ok", "ChlParam": arr})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("蓝电协议模拟服务器已启动： http://127.0.0.1:%d  (Ctrl+C 退出)" % args.port)
    print("接口: /OpearMon/control  /QueryChlStatus/control  /QueryChlRP/control")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
