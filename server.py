#!/usr/bin/env python3
import json
import time
import threading
import urllib.request
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ============================================================
COOKIE = (
    "did=web_2181dc63fe687fe78a9dd9dd572b8ed8; "
    "kwpsecproductname=kuaishou-vision; "
    "didv=1784348502126; "
    "userId=2460229092; "
    "kwfv1=PeDA80mSG00ZF8e400wnrU+fr78fLAwn+f+erh8nz0PfrAPfr98fGE+nL9P/DFPADUP/LU+nLA8ecU8fcF+0rI+9cl80YS8nHUP/+0+/mfG9QD+/pfG9LUweDAwnpj8BrA+/Z9+ASDw/DlPADE+n+jG/chGAc7wnH9+AWE+n+jPfH9wncFP/DA+0LU8BHF+080+eDhP/DAPfcE8eqh+/cFPZ=="
)

COURSES = [
    {"id": "3086099", "label": "登陆少年组合"},
    {"id": "3086098", "label": "左航"},
    {"id": "3086097", "label": "苏新皓"},
    {"id": "3086096", "label": "张泽禹"},
    {"id": "3086095", "label": "朱志鑫"},
    {"id": "3086102", "label": "张极"},
]

UPDATE_INTERVAL = 600
PORT = int(os.environ.get("PORT", 8080))

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://nwnawzlrtpiqmeyowzla.supabase.co"
)
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY", "sb_publishable_sXR25OlLuSsfK2JDSCZWXw_7HOVXkxS"
)
# ============================================================

cache = {"data": [], "updated_at": None, "status": "loading"}

def parse_sold_number(value):
    if value is None:
        return None
    
    # 统一转为字符串，并把西方计数法的逗号去掉（比如 1,234 -> 1234）
    text = str(value).replace(",", "")
    
    # 使用正则表达式进行暴力提取：
    # ([\d\.]+) 负责抓取连续的数字（包括小数点）
    # \s* 兼容数字和单位之间可能存在的不可见空格
    # ([万千wWkK]?) 负责抓取可能跟随在数字后面的单位
    match = re.search(r'([\d\.]+)\s*([万千wWkK]?)', text)
    
    if not match:
        return None
        
    num_str = match.group(1)
    unit = match.group(2).lower()
    
    try:
        # 先转化为浮点数处理小数情况
        num = float(num_str)
        
        if unit in ['万', 'w']:
            return int(num * 10000)
        elif unit in ['千', 'k']:
            return int(num * 1000)
        else:
            return int(num)
            
    except Exception:
        # 如果遇到无法转化为 float 的极端异常字符串，安全返回 None
        return None


def fetch_course(course_id):
    url = (
        "https://m-vision.ketang.kuaishou.com/rest/lightks/wd/v2/course/trade/info"
        f"?courseId={course_id}"
    )
    req = urllib.request.Request(url)
    req.add_header("Accept", "*/*")
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("Cookie", COOKIE)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    )
    req.add_header(
        "Referer", f"https://m-vision.ketang.kuaishou.com/detail/{course_id}"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode())
    if body.get("result") != 1:
        raise Exception(f"接口返回错误码: {body.get('result')}")

    trade = body.get("data", {}).get("courseTradeInfo", {})
    sku = trade.get("skuList", [{}])[0]
    sold = parse_sold_number(sku.get("displaySoldNumber") or trade.get("displaySoldNumber"))
    if sold is None:
        raise Exception("displaySoldNumber 为空")

    return {
        "soldNumber": sold,
        "unitPrice": sku.get("unitPrice"),
        "kscoinPrice": sku.get("kscoinPrice"),
    }


def save_to_supabase(results):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    rows = [
        {"course_id": r["id"], "label": r["label"], "sold_number": r["soldNumber"]}
        for r in results
        if r["soldNumber"] is not None
    ]
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/sales_history"
    data = json.dumps(rows).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Prefer", "return=minimal")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"  已保存 {len(rows)} 条记录到 Supabase")
    except Exception as e:
        print(f"  Supabase 写入失败: {e}")


def fetch_history():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = (
        f"{SUPABASE_URL}/rest/v1/sales_history"
        "?select=course_id,label,sold_number,recorded_at&order=recorded_at.asc"
    )
    req = urllib.request.Request(url)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  Supabase 读取失败: {e}")
        return []

def fetch_all():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始抓取数据...")
    results = []
    for course in COURSES:
        success = False
        last_error = "未知错误"  # 新增：用于记录真实的报错信息
        
        for attempt in range(3):
            try:
                data = fetch_course(course["id"])
                results.append({**course, **data, "error": None})
                print(f"  ✓ {course['label']}: {data['soldNumber']}")
                success = True
                break
            except Exception as e:
                last_error = str(e)  # 捕获每一次失败的具体原因
                print(f"  第{attempt + 1}次失败 {course['label']}: {e}")
                time.sleep(3)
                
        if not success:
            results.append(
                {
                    **course,
                    "soldNumber": None,
                    "unitPrice": None,
                    "kscoinPrice": None,
                    # 下面这行最关键，把真实的错误抛到网页前端
                    "error": f"抓取失败: {last_error}",
                }
            )
        time.sleep(2)

    cache["data"] = results
    cache["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cache["status"] = "ok"
    save_to_supabase(results)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 抓取完成\n")



def background_loop():
    last_crawl_minute = -1
    last_hour_crawl = -1
    while True:
        now = datetime.now()
        current_minute = now.hour * 60 + now.minute
        if now.minute == 0 and now.hour != last_hour_crawl:
            fetch_all()
            last_hour_crawl = now.hour
            last_crawl_minute = current_minute
        elif current_minute - last_crawl_minute >= 10:
            fetch_all()
            last_crawl_minute = current_minute
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/api/sales":
            body = json.dumps(cache, ensure_ascii=False).encode()
            self._respond(200, "application/json", body)

        elif self.path == "/api/history":
            data = fetch_history()
            body = json.dumps(data, ensure_ascii=False).encode()
            self._respond(200, "application/json", body)

        elif self.path in ("/", "/index.html"):
            try:
                with open("index.html", "rb") as f:
                    self._respond(200, "text/html; charset=utf-8", f.read())
            except FileNotFoundError:
                self._respond(404, "text/plain", b"index.html not found")
        else:
            self._respond(404, "text/plain", b"Not found")


    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        
        # 👇 新增下面这一行，彻底禁用 API 的缓存
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
        
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


if __name__ == "__main__":
    print("=" * 50)
    print("  登陆少年梦寐以求演唱会直拍机位 · 销量监控")
    print(f"  http://localhost:{PORT}")
    print("=" * 50)
    fetch_all()
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
