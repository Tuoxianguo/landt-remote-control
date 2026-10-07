# -*- coding: utf-8 -*-
"""
===============================================================================
 蓝电(LAND)测试柜远程控制上位机  LANDT Remote Control
-------------------------------------------------------------------------------
 实现《蓝电远程控制及数据获取协议》全部 3 个 HTTP 接口：
   1) /OpearMon/control     远程 启动/续接/停止 通道（含电池条码、活性质量、比容量、极性、备份方案）
   2) /QueryChlStatus/control  获取通道状态（测试中/停止/完成/故障/无状态）
   3) /QueryChlRP/control    获取通道实时测试参数（电压/电流/容量/循环/工步/温度等）

 通讯架构：
   测试柜(硬件) --COM7--> 蓝电监控软件(LANDMon/CLANDTestDlg, 本机HTTP服务:7777) --HTTP--> 本程序

 通道号规则：
   nBox = 箱号(基于1)   nChl = 通道号(基于1, 范围[1,8])
   8 通道即 nBox=1, nChl=1..8，通道标识形如 "001_8"(箱001 通道8)

 作者：自动生成（可直接打包为 exe 在其它电脑使用）
===============================================================================
"""
import os
import sys
import json
import time
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

APP_TITLE = "蓝电(LAND)测试柜远程控制上位机 v1.0"
CONFIG_FILE = "landt_config.json"
CHANNEL_COUNT = 8  # 1*8 通道

# 通道状态映射（协议 2.2 / 2.3）：0/1/2/3/4 => 测试中/停止/完成/故障/无状态
STATUS_TEXT = {0: "测试中", 1: "停止", 2: "完成", 3: "故障", 4: "无状态"}
STATUS_BG = {
    0: "#2e7d32",   # 测试中 绿
    1: "#757575",   # 停止   灰
    2: "#1565c0",   # 完成   蓝
    3: "#c62828",   # 故障   红
    4: "#bdbdbd",   # 无状态 浅灰
    None: "#424242",  # 未知
}

# 操作类型（协议 2.1）：0/1/2 => 启动/续接/停止
OPERA_START = 0
OPERA_RESUME = 1
OPERA_STOP = 2


# -----------------------------------------------------------------------------
#  配置文件读写（保存在 exe/脚本同目录，换电脑后设置可持久化）
# -----------------------------------------------------------------------------
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), CONFIG_FILE)


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# -----------------------------------------------------------------------------
#  本机所有 IPv4 地址（用于自动探测 LANDMon 绑定的主机）
# -----------------------------------------------------------------------------
def local_ipv4s():
    ips = []
    seen = set()

    def add(ip):
        if ip and ip not in seen and not ip.startswith("127."):
            seen.add(ip)
            ips.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


# -----------------------------------------------------------------------------
#  协议客户端：封装 3 个 HTTP 接口
# -----------------------------------------------------------------------------
class LandClient:
    # ---- 全局请求节流：LANDMon 服务端有防频繁请求限流(QC) ----
    #  所有请求串行化，并保证相邻请求间隔 >= MIN_GAP 秒，避免被服务端拒绝。
    _lock = threading.Lock()
    _last_ts = [0.0]
    MIN_GAP = 1.2          # 相邻请求最小间隔(秒)
    QC_RETRY = 4           # 命中限流后的重试次数
    QC_BACKOFF = 0.9       # 限流退避基数(秒)

    def __init__(self, host="127.0.0.1", port=7777, timeout=5.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    @staticmethod
    def _is_qc(data):
        return (isinstance(data, dict) and data.get("nCode") == -1
                and "frequent" in str(data.get("strMsg", "")).lower())

    def _post_once(self, url, payload):
        # 串行 + 节流：整段持锁，确保任意两次请求不并发且有最小间隔
        with LandClient._lock:
            gap = time.time() - LandClient._last_ts[0]
            if gap < LandClient.MIN_GAP:
                time.sleep(LandClient.MIN_GAP - gap)
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:
                    return json.loads(resp.text)
            finally:
                LandClient._last_ts[0] = time.time()

    def _post(self, path, payload):
        """发送 POST(JSON) 请求；命中限流(QC)时自动退避重试。"""
        if requests is None:
            raise RuntimeError("缺少 requests 库，无法通讯")
        url = self.base_url() + path
        data = None
        for attempt in range(LandClient.QC_RETRY):
            data = self._post_once(url, payload)
            if not self._is_qc(data):
                return data
            time.sleep(LandClient.QC_BACKOFF * (attempt + 1))
        return data  # 多次仍被限流，返回最后一次结果由上层提示

    # ---- 接口1：远程 启动/续接/停止 ----
    def control(self, nOperaType, chls, cyc_reverse=0, backup_dir="",
                spi_path="", scheme_flag="1210", part_a=""):
        payload = {
            "nOperaType": int(nOperaType),
            "CycModeReverse": int(cyc_reverse),
            "strBackupDir": backup_dir or "",
            "strSpiFullPath": spi_path or "",
            "SchemeFlag": scheme_flag or "1210",
            "PartA": part_a or "",
            "MultipleChls": chls,
        }
        return self._post("/OpearMon/control", payload)

    # ---- 接口2：获取通道状态 ----
    def query_status(self):
        return self._post("/QueryChlStatus/control", {})

    # ---- 接口3：获取通道测试参数 ----
    def query_rp(self, chls=None):
        """chls=None 或 [] 查询所有通道；否则按 [{nBox,nChl}] 查询指定通道。"""
        payload = {"vecAFO": chls or []}
        return self._post("/QueryChlRP/control", payload)


def extract_array(resp, key):
    """兼容两种返回：顶层数组字段 或 strMsg 内嵌 JSON。"""
    if isinstance(resp, dict):
        if isinstance(resp.get(key), list):
            return resp[key]
        msg = resp.get("strMsg")
        if isinstance(msg, str):
            try:
                inner = json.loads(msg)
                if isinstance(inner, dict) and isinstance(inner.get(key), list):
                    return inner[key]
                if isinstance(inner, list):
                    return inner
            except Exception:
                pass
    return []


# -----------------------------------------------------------------------------
#  主界面
# -----------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x760")
        self.minsize(1120, 680)

        self.cfg = load_config()
        self.client = None
        self.auto_job = None
        self.ui_queue = queue.Queue()

        # 每个通道的控件引用
        self.rows = []  # list of dict

        self._build_style()
        self._build_connection_bar()
        self._build_shared_params()
        self._build_channel_table()
        self._build_log()

        self._load_values_from_cfg()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain_queue)

    # ---------- 样式 ----------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=4)
        style.configure("TEntry", font=("Consolas", 10))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=6)

    # ---------- 连接栏 ----------
    def _build_connection_bar(self):
        bar = ttk.LabelFrame(self, text="连接设置（蓝电监控软件 HTTP 服务）")
        bar.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(bar, text="主机IP:").grid(row=0, column=0, padx=(8, 2), pady=6, sticky="e")
        self.var_host = tk.StringVar(value="127.0.0.1")
        ttk.Entry(bar, textvariable=self.var_host, width=16).grid(row=0, column=1, pady=6)

        ttk.Label(bar, text="端口:").grid(row=0, column=2, padx=(10, 2), pady=6, sticky="e")
        self.var_port = tk.StringVar(value="7777")
        ttk.Entry(bar, textvariable=self.var_port, width=8).grid(row=0, column=3, pady=6)

        ttk.Button(bar, text="自动查找主机", command=self.on_auto_find).grid(row=0, column=4, padx=(10, 2))
        ttk.Button(bar, text="测试连接", command=self.on_test_conn).grid(row=0, column=5, padx=2)

        self.var_conn = tk.StringVar(value="● 未连接")
        self.lbl_conn = ttk.Label(bar, textvariable=self.var_conn, foreground="#c62828")
        self.lbl_conn.grid(row=0, column=6, padx=10)

        # 自动刷新
        ttk.Separator(bar, orient="vertical").grid(row=0, column=7, sticky="ns", padx=10)
        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动刷新", variable=self.var_auto,
                        command=self.on_toggle_auto).grid(row=0, column=8, padx=4)
        ttk.Label(bar, text="间隔(秒):").grid(row=0, column=9, sticky="e")
        self.var_interval = tk.StringVar(value="5")
        ttk.Spinbox(bar, from_=1, to=60, width=5, textvariable=self.var_interval).grid(row=0, column=10, padx=(2, 8))

        ttk.Button(bar, text="立即刷新", command=self.refresh_once).grid(row=0, column=11, padx=6)

    # ---------- 共享参数 ----------
    def _build_shared_params(self):
        box = ttk.LabelFrame(self, text="启动参数（启动时作用于所有选中通道；停止/续接可留空）")
        box.pack(fill="x", padx=8, pady=4)

        # 第一行
        ttk.Label(box, text="箱号 nBox:").grid(row=0, column=0, padx=(8, 2), pady=6, sticky="e")
        self.var_box = tk.StringVar(value="1")
        self.cmb_box = ttk.Combobox(box, textvariable=self.var_box, width=5, values=["1"])
        self.cmb_box.grid(row=0, column=1, pady=6, sticky="w")
        self.cmb_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_once())

        ttk.Label(box, text="极性:").grid(row=0, column=2, padx=(10, 2), sticky="e")
        self.var_polarity = tk.StringVar(value="正极性")
        ttk.Combobox(box, textvariable=self.var_polarity, width=8, state="readonly",
                     values=["正极性", "反极性"]).grid(row=0, column=3, sticky="w")

        ttk.Label(box, text="SchemeFlag:").grid(row=0, column=4, padx=(10, 2), sticky="e")
        self.var_scheme = tk.StringVar(value="1210")
        ttk.Entry(box, textvariable=self.var_scheme, width=8).grid(row=0, column=5, sticky="w")
        ttk.Button(box, text="?", width=2, command=self.show_scheme_help).grid(row=0, column=6, padx=(2, 6))

        ttk.Label(box, text="PartA前缀:").grid(row=0, column=7, padx=(10, 2), sticky="e")
        self.var_parta = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.var_parta, width=18).grid(row=0, column=8, sticky="w")

        # 第二行：工艺文件 spi
        ttk.Label(box, text="测试工艺(.spi):").grid(row=1, column=0, padx=(8, 2), pady=6, sticky="e")
        self.var_spi = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.var_spi, width=58).grid(row=1, column=1, columnspan=6, pady=6, sticky="we")
        ttk.Button(box, text="浏览…", command=self.on_pick_spi).grid(row=1, column=7, padx=4, sticky="w")

        # 第三行：备份目录
        ttk.Label(box, text="备份目录:").grid(row=2, column=0, padx=(8, 2), pady=6, sticky="e")
        self.var_backup = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.var_backup, width=58).grid(row=2, column=1, columnspan=6, pady=6, sticky="we")
        ttk.Button(box, text="浏览…", command=self.on_pick_backup).grid(row=2, column=7, padx=4, sticky="w")

        box.columnconfigure(8, weight=1)

    # ---------- 通道表格（1*8，每行一个通道）----------
    def _build_channel_table(self):
        outer = ttk.LabelFrame(self, text="通道列表（8 通道 1×8；可勾选后批量 启动/续接/停止）")
        outer.pack(fill="both", expand=True, padx=8, pady=4)

        # 操作按钮行
        btns = ttk.Frame(outer)
        btns.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(btns, text="全选", command=lambda: self.select_all(True)).pack(side="left", padx=2)
        ttk.Button(btns, text="全不选", command=lambda: self.select_all(False)).pack(side="left", padx=2)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(btns, text="▶ 启动选中", style="Big.TButton",
                   command=self.on_start).pack(side="left", padx=4)
        ttk.Button(btns, text="⏯ 续接选中", style="Big.TButton",
                   command=self.on_resume).pack(side="left", padx=4)
        ttk.Button(btns, text="■ 停止选中", style="Big.TButton",
                   command=self.on_stop).pack(side="left", padx=4)

        # 表头
        grid = ttk.Frame(outer)
        grid.pack(fill="both", expand=True, padx=6, pady=4)

        headers = ["选", "通道标识", "条码(strBattNo)", "活性质量ActMass",
                   "比容量SpeCap", "状态", "电压V", "电流mA", "容量",
                   "循环", "工步", "工步时间", "温度", "备份文件名"]
        widths = [3, 10, 20, 12, 12, 8, 10, 10, 12, 6, 5, 12, 7, 24]
        for c, (h, w) in enumerate(zip(headers, widths)):
            ttk.Label(grid, text=h, style="Header.TLabel",
                      anchor="center").grid(row=0, column=c, padx=2, pady=3, sticky="nsew")
            grid.columnconfigure(c, minsize=w * 8)

        self.rows = []
        for i in range(CHANNEL_COUNT):
            chl = i + 1
            r = i + 1
            row = {}

            row["sel"] = tk.BooleanVar(value=False)
            ttk.Checkbutton(grid, variable=row["sel"]).grid(row=r, column=0, padx=2, pady=2)

            row["ident"] = tk.StringVar(value="001_%d" % chl)
            ttk.Label(grid, textvariable=row["ident"], anchor="center").grid(row=r, column=1, sticky="nsew")

            row["batt"] = tk.StringVar(value="")
            ttk.Entry(grid, textvariable=row["batt"], width=18).grid(row=r, column=2, padx=2, sticky="we")

            row["mass"] = tk.StringVar(value="")
            ttk.Entry(grid, textvariable=row["mass"], width=10).grid(row=r, column=3, padx=2, sticky="we")

            row["spec"] = tk.StringVar(value="")
            ttk.Entry(grid, textvariable=row["spec"], width=10).grid(row=r, column=4, padx=2, sticky="we")

            row["status"] = tk.StringVar(value="—")
            lbl = tk.Label(grid, textvariable=row["status"], width=7,
                           fg="white", bg=STATUS_BG[None],
                           font=("Microsoft YaHei UI", 9, "bold"))
            lbl.grid(row=r, column=5, padx=2, pady=2, sticky="nsew")
            row["status_lbl"] = lbl

            row["volt"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["volt"], anchor="e").grid(row=r, column=6, padx=2, sticky="nsew")
            row["curr"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["curr"], anchor="e").grid(row=r, column=7, padx=2, sticky="nsew")
            row["cap"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["cap"], anchor="e").grid(row=r, column=8, padx=2, sticky="nsew")
            row["loop"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["loop"], anchor="center").grid(row=r, column=9, padx=2, sticky="nsew")
            row["step"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["step"], anchor="center").grid(row=r, column=10, padx=2, sticky="nsew")
            row["stepspan"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["stepspan"], anchor="center").grid(row=r, column=11, padx=2, sticky="nsew")
            row["temp"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["temp"], anchor="e").grid(row=r, column=12, padx=2, sticky="nsew")
            row["cex"] = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=row["cex"], anchor="w").grid(row=r, column=13, padx=2, sticky="nsew")

            row["chl"] = chl
            self.rows.append(row)

    # ---------- 日志 ----------
    def _build_log(self):
        box = ttk.LabelFrame(self, text="运行日志")
        box.pack(fill="both", padx=8, pady=(4, 8))
        self.txt_log = tk.Text(box, height=8, font=("Consolas", 9), wrap="word")
        self.txt_log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(box, command=self.txt_log.yview)
        sb.pack(side="right", fill="y", pady=6)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.log("程序已启动。请确认蓝电监控软件(LANDMon/CLANDTestDlg)已运行，并已通过 COM 口连接测试柜。")

    # =========================================================================
    #  工具方法
    # =========================================================================
    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", "[%s] %s\n" % (ts, msg))
        self.txt_log.see("end")

    def build_client(self):
        host = self.var_host.get().strip() or "127.0.0.1"
        try:
            port = int(self.var_port.get().strip())
        except ValueError:
            port = 7777
        self.client = LandClient(host, port)
        return self.client

    def polarity_value(self):
        return 1 if self.var_polarity.get() == "反极性" else 0

    def box_value(self):
        try:
            return int(self.var_box.get().strip())
        except ValueError:
            return 1

    def ident_of(self, chl):
        return "%03d_%d" % (self.box_value(), chl)

    def selected_rows(self):
        return [r for r in self.rows if r["sel"].get()]

    def select_all(self, flag):
        for r in self.rows:
            r["sel"].set(flag)

    # =========================================================================
    #  连接 / 刷新
    # =========================================================================
    def on_test_conn(self):
        self._do_connect(auto=False)

    def on_auto_find(self):
        self._do_connect(auto=True)

    def _candidate_hosts(self):
        cur = self.var_host.get().strip()
        cands = []
        if cur:
            cands.append(cur)
        for h in (["127.0.0.1"] + local_ipv4s()):
            if h not in cands:
                cands.append(h)
        return cands

    @staticmethod
    def _boxes_from_status(resp):
        """返回 {箱号: 是否有有效通道(非无状态)}。"""
        boxes = {}
        for it in extract_array(resp, "AllChlStatus"):
            try:
                b = int(it.get("nBox"))
                s = int(it.get("nStatus"))
            except Exception:
                continue
            boxes.setdefault(b, False)
            if s != 4:  # 4=无状态；非4视为该箱存在有效通道
                boxes[b] = True
        return boxes

    def _do_connect(self, auto):
        try:
            port = int(self.var_port.get().strip())
        except ValueError:
            port = 7777
        if auto:
            hosts = self._candidate_hosts()
        else:
            hosts = [self.var_host.get().strip() or "127.0.0.1"]
        self.log("正在%s连接 [%s]:%d …" % ("自动查找并" if auto else "", " / ".join(hosts), port))

        def work():
            last_err = None
            for h in hosts:
                try:
                    c = LandClient(h, port, timeout=1.5)
                    resp = c.query_status()
                    if isinstance(resp, dict) and "nCode" in resp:
                        boxes = self._boxes_from_status(resp)
                        self.ui_queue.put(("host_found", h, boxes))
                        return
                except Exception as e:
                    last_err = e
            self.ui_queue.put(("conn", False,
                               "连接失败：%s\n（请确认 LANDMon 已运行，且主机/端口正确）" % last_err))
        threading.Thread(target=work, daemon=True).start()

    def _populate_boxes(self, boxes, auto_switch=True):
        if not boxes:
            return
        all_boxes = sorted(boxes.keys())
        active = [b for b in all_boxes if boxes[b]]
        self.cmb_box["values"] = [str(b) for b in all_boxes]
        if not auto_switch:
            return
        cur = self.var_box.get().strip()
        if active and (not cur.isdigit() or int(cur) not in active):
            self.var_box.set(str(active[0]))
            self.log("检测到有效箱体：%s；已自动选择箱 %d" %
                     (",".join(str(b) for b in active), active[0]))
        elif active:
            self.log("检测到有效箱体：%s（当前选择箱 %s）" %
                     (",".join(str(b) for b in active), cur))
        else:
            self.log("未检测到有效通道（所有通道均为“无状态”），请确认设备已上电/已连接、箱号是否正确。")

    def on_toggle_auto(self):
        if self.var_auto.get():
            self.schedule_auto()
        else:
            if self.auto_job:
                self.after_cancel(self.auto_job)
                self.auto_job = None

    def schedule_auto(self):
        if self.auto_job:
            self.after_cancel(self.auto_job)
        try:
            interval = max(1, int(self.var_interval.get()))
        except ValueError:
            interval = 2
        self.refresh_once()
        self.auto_job = self.after(interval * 1000, self.schedule_auto)

    def refresh_once(self):
        self.build_client()
        box = self.box_value()
        chls = [{"nBox": box, "nChl": c} for c in range(1, CHANNEL_COUNT + 1)]

        def work():
            data = {}
            ok = False
            qc = False
            try:
                st = self.client.query_status()
                if LandClient._is_qc(st):
                    qc = True
                data["status"] = extract_array(st, "AllChlStatus")
                data["boxes"] = self._boxes_from_status(st)
                ok = True
            except Exception as e:
                self.ui_queue.put(("log", "查询状态失败：%s" % e))
            try:
                rp = self.client.query_rp(chls)
                if LandClient._is_qc(rp):
                    qc = True
                data["rp"] = extract_array(rp, "ChlParam")
                ok = True
            except Exception as e:
                self.ui_queue.put(("log", "查询参数失败：%s" % e))
            if qc:
                self.ui_queue.put(("qc", None))
            self.ui_queue.put(("conn", ok, None))  # 刷新时只更新指示灯，不刷屏
            self.ui_queue.put(("refresh", data))
        threading.Thread(target=work, daemon=True).start()

    # =========================================================================
    #  数据回填
    # =========================================================================
    def _apply_refresh(self, data):
        # 刷新箱体下拉列表(不切换用户当前选择)
        if data.get("boxes"):
            self._populate_boxes(data["boxes"], auto_switch=False)

        # 先按通道号建立索引
        by_chl = {r["chl"]: r for r in self.rows}

        # 状态
        for item in data.get("status", []) or []:
            try:
                chl = int(item.get("nChl"))
                box = int(item.get("nBox", self.box_value()))
            except Exception:
                continue
            if box != self.box_value():
                continue
            row = by_chl.get(chl)
            if not row:
                continue
            ns = item.get("nStatus")
            try:
                ns = int(ns)
            except Exception:
                ns = None
            row["status"].set(STATUS_TEXT.get(ns, "未知"))
            row["status_lbl"].configure(bg=STATUS_BG.get(ns, STATUS_BG[None]))

        # 参数
        for item in data.get("rp", []) or []:
            b, chl = self._parse_chl_field(item.get("Chl"))
            if b is not None and b != self.box_value():
                continue
            row = by_chl.get(chl)
            if not row:
                continue
            row["volt"].set(self._fmt(item.get("Volt"), 4))
            row["curr"].set(self._fmt(item.get("Curr"), 3))
            row["cap"].set(self._fmt(item.get("Cap"), 4))
            row["loop"].set(self._fmt(item.get("Loop"), 0))
            row["step"].set(self._fmt(item.get("StepNo"), 0))
            row["stepspan"].set(str(item.get("StepSpan") or "—"))
            row["temp"].set(self._fmt(item.get("Temperature"), 1))
            row["cex"].set(str(item.get("CexName") or "—"))
            # 若状态接口没返回，用参数里的 Status 兜底
            if row["status"].get() in ("—", "未知"):
                ns = item.get("Status")
                try:
                    ns = int(ns)
                except Exception:
                    ns = None
                row["status"].set(STATUS_TEXT.get(ns, "未知"))
                row["status_lbl"].configure(bg=STATUS_BG.get(ns, STATUS_BG[None]))

    def _parse_chl_field(self, chl_str):
        """解析 '002_1' 形式，返回 (箱号, 通道号)。"""
        if chl_str is None:
            return (None, None)
        s = str(chl_str)
        if "_" in s:
            parts = s.split("_")
            try:
                return (int(parts[0]), int(parts[-1]))
            except Exception:
                return (None, None)
        try:
            return (None, int(s))
        except Exception:
            return (None, None)

    @staticmethod
    def _fmt(v, nd):
        if v is None:
            return "—"
        try:
            if nd == 0:
                return str(int(float(v)))
            return ("%." + str(nd) + "f") % float(v)
        except Exception:
            return str(v)

    # =========================================================================
    #  启动 / 续接 / 停止
    # =========================================================================
    def _collect_chls(self, need_start_fields):
        """收集选中通道的参数列表。need_start_fields=True 时带条码/质量/比容量。"""
        chls = []
        box = self.box_value()
        for r in self.selected_rows():
            item = {"nBox": box, "nChl": r["chl"]}
            if need_start_fields:
                item["strBattNo"] = r["batt"].get().strip()
                mass = r["mass"].get().strip()
                spec = r["spec"].get().strip()
                if mass:
                    try:
                        item["ActMass"] = float(mass)
                    except ValueError:
                        pass
                if spec:
                    try:
                        item["SpeCap"] = float(spec)
                    except ValueError:
                        pass
            else:
                item["strBattNo"] = ""
            chls.append(item)
        return chls

    def on_start(self):
        rows = self.selected_rows()
        if not rows:
            messagebox.showwarning("提示", "请先勾选要启动的通道。")
            return
        spi = self.var_spi.get().strip()
        if not spi:
            messagebox.showwarning("提示", "启动必须选择测试工艺(.spi)文件。")
            return
        if not os.path.isfile(spi):
            if not messagebox.askyesno("确认",
                                       "工艺文件在本机不存在：\n%s\n\n"
                                       "（该路径需在【蓝电软件所在电脑】上存在）\n是否仍继续发送？" % spi):
                return
        scheme = self.var_scheme.get().strip()
        if len(scheme) != 4 or not scheme.isdigit():
            messagebox.showwarning("提示", "SchemeFlag 必须是 4 位数字，例如 1210。")
            return
        chls = self._collect_chls(need_start_fields=True)
        self._send_control(OPERA_START, chls, "启动")

    def on_resume(self):
        rows = self.selected_rows()
        if not rows:
            messagebox.showwarning("提示", "请先勾选要续接的通道。")
            return
        chls = self._collect_chls(need_start_fields=False)
        self._send_control(OPERA_RESUME, chls, "续接")

    def on_stop(self):
        rows = self.selected_rows()
        if not rows:
            messagebox.showwarning("提示", "请先勾选要停止的通道。")
            return
        if not messagebox.askyesno("确认停止", "确定要停止选中的 %d 个通道吗？" % len(rows)):
            return
        chls = self._collect_chls(need_start_fields=False)
        self._send_control(OPERA_STOP, chls, "停止")

    def _send_control(self, opera, chls, name):
        self.build_client()
        idents = ", ".join(self.ident_of(c["nChl"]) for c in chls)
        self.log("正在%s通道：%s …" % (name, idents))

        cyc = self.polarity_value()
        backup = self.var_backup.get().strip()
        spi = self.var_spi.get().strip()
        scheme = self.var_scheme.get().strip() or "1210"
        parta = self.var_parta.get().strip()

        def work():
            try:
                resp = self.client.control(opera, chls, cyc_reverse=cyc,
                                           backup_dir=backup, spi_path=spi,
                                           scheme_flag=scheme, part_a=parta)
                code = resp.get("nCode") if isinstance(resp, dict) else None
                msg = resp.get("strMsg") if isinstance(resp, dict) else str(resp)
                if code == 0:
                    self.ui_queue.put(("log", "[成功] %s成功 (nCode=0) %s" % (name, msg or "")))
                else:
                    self.ui_queue.put(("log", "[警告] %s返回 nCode=%s: %s" % (name, code, msg or "")))
            except Exception as e:
                self.ui_queue.put(("log", "[失败] %s失败：%s" % (name, e)))
            self.ui_queue.put(("refresh_req", None))
        threading.Thread(target=work, daemon=True).start()

    # =========================================================================
    #  文件选择 / 帮助
    # =========================================================================
    def on_pick_spi(self):
        p = filedialog.askopenfilename(title="选择测试工艺文件",
                                       filetypes=[("蓝电工艺文件", "*.spi"), ("所有文件", "*.*")])
        if p:
            self.var_spi.set(p)

    def on_pick_backup(self):
        p = filedialog.askdirectory(title="选择备份目录")
        if p:
            self.var_backup.set(p)

    def show_scheme_help(self):
        txt = (
            "SchemeFlag 为 4 位数字，用于组合备份文件名：\n\n"
            "第1位 0/1 => 启用/弃用 PartA(前缀名)\n"
            "第2位 0/1/2/3 => 弃用PartB / 通道启动日期 / 电池条码 / 电池条码_通道启动时刻\n"
            "第3位 0/1/2 => 弃用PartC / 通道号(如 _003_8) / PartC_Alias\n"
            "第4位 0/1 => 名字顺序 AB_C / A_C_B\n\n"
            "示例 1210：弃用PartA、用电池条码作PartB、用通道号作PartC、顺序AB_C\n"
            "→ 备份文件名形如：电池条码_001_8"
        )
        messagebox.showinfo("SchemeFlag 说明", txt)

    # =========================================================================
    #  配置持久化
    # =========================================================================
    def _load_values_from_cfg(self):
        c = self.cfg
        self.var_host.set(c.get("host", "127.0.0.1"))
        self.var_port.set(str(c.get("port", 7777)))
        self.var_box.set(str(c.get("box", 1)))
        self.var_polarity.set(c.get("polarity", "正极性"))
        self.var_scheme.set(c.get("scheme", "1210"))
        self.var_parta.set(c.get("parta", ""))
        self.var_spi.set(c.get("spi", ""))
        self.var_backup.set(c.get("backup", ""))
        self.var_interval.set(str(c.get("interval", 5)))
        batts = c.get("batts", [])
        for i, r in enumerate(self.rows):
            if i < len(batts):
                r["batt"].set(batts[i])
        # 启动后自动查找主机(含局域网IP)并扫描箱体，再开始刷新
        self.after(400, self.on_auto_find)

    def _save_values_to_cfg(self):
        self.cfg.update({
            "host": self.var_host.get().strip(),
            "port": self.var_port.get().strip(),
            "box": self.var_box.get().strip(),
            "polarity": self.var_polarity.get(),
            "scheme": self.var_scheme.get().strip(),
            "parta": self.var_parta.get().strip(),
            "spi": self.var_spi.get().strip(),
            "backup": self.var_backup.get().strip(),
            "interval": self.var_interval.get().strip(),
            "batts": [r["batt"].get() for r in self.rows],
        })
        save_config(self.cfg)

    # =========================================================================
    #  UI 队列 & 关闭
    # =========================================================================
    def _drain_queue(self):
        try:
            while True:
                kind, *rest = self.ui_queue.get_nowait()
                if kind == "log":
                    self.log(rest[0])
                elif kind == "conn":
                    ok, msg = rest
                    self.var_conn.set("● 已连接" if ok else "● 未连接")
                    self.lbl_conn.configure(foreground="#2e7d32" if ok else "#c62828")
                    if msg:
                        self.log(msg)
                elif kind == "host_found":
                    host, boxes = rest
                    self.var_host.set(host)
                    self.var_conn.set("● 已连接")
                    self.lbl_conn.configure(foreground="#2e7d32")
                    self.log("连接成功：主机 %s:%s" % (host, self.var_port.get().strip()))
                    self._populate_boxes(boxes)
                    if self.var_auto.get():
                        self.schedule_auto()
                    else:
                        self.refresh_once()
                elif kind == "qc":
                    now = time.time()
                    if now - getattr(self, "_qc_last_log", 0) > 20:
                        self._qc_last_log = now
                        self.log("提示：服务端触发防频繁请求限流(QC)，已自动退避重试；"
                                 "如仍频繁出现，请把【刷新间隔】调大(如 8~10 秒)。")
                elif kind == "refresh":
                    self._apply_refresh(rest[0])
                elif kind == "refresh_req":
                    self.refresh_once()
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def on_close(self):
        self._save_values_to_cfg()
        if self.auto_job:
            try:
                self.after_cancel(self.auto_job)
            except Exception:
                pass
        self.destroy()


def main():
    if requests is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("缺少依赖", "未找到 requests 库。\n请运行: pip install requests")
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
