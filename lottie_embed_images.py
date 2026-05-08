#!/usr/bin/env python3
"""
Lottie Embed Images

Copyright (C) 2026 griboed256

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

-----------------------------
lottie_embed_images.py (Smart Batch + Validation)
Упаковывает Lottie JSON + PNG-секвенцию в один self-contained файл.
Добавлена защита от неверных файлов (валидация).
"""

import sys
import subprocess
import importlib
import multiprocessing
import json
import base64
import io
import time
import os
import threading
import queue
import webbrowser
import http.server
import socketserver
import socket
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Callable, Dict, Tuple, List, Any
from assets import get_lottie_js

# ===========================================================================
# ГЛОБАЛЬНЫЕ НАСТРОЙКИ
# ===========================================================================
_RUNNING_AS_BUNDLE = getattr(sys, "frozen", False)
APP_VERSION = "v.1.2.9"
APP_BUILD = "v.1.2.9_1155_080526"
QUALITY_WARNING_THRESHOLD = 30

# ===========================================================================
# БЛОК 1 — Логирование и утилиты ОС
# ===========================================================================
def _setup_debug_log() -> None:
    if _RUNNING_AS_BUNDLE:
        return
    try:
        import __main__
        script_dir = Path(getattr(__main__, '__file__', '.')).parent
        log_path = script_dir / "lottie_debug.log"
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
            handlers=[
                logging.FileHandler(log_path, encoding="utf-8", mode="w"),
                logging.StreamHandler(sys.stdout),
            ]
        )
        logging.info("=== Lottie Embed Images запущен ===")
        logging.info(f"Python {sys.version_info.major}.{sys.version_info.minor} | bundle={_RUNNING_AS_BUNDLE}")
    except Exception as e:
        print(f"Failed to setup logging: {e}")

def dbg(msg: str) -> None:
    try:
        logging.debug(msg)
    except Exception:
        pass

def enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception as e:
            dbg(f"DPI Awareness error: {e}")

_setup_debug_log()

# ===========================================================================
# БЛОК 2 — Бизнес-логика (Модели и Сервисы)
# ===========================================================================

class ImageUtils:
    @staticmethod
    def get_mime_type(file_path: Path) -> str:
        return {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(file_path.suffix.lower(), "image/png")

    @staticmethod
    def format_size(b: float) -> str:
        if b >= 1 << 20:
            return f"{b / (1 << 20):.2f} MB"
        return f"{b / 1024:.1f} KB"

    @staticmethod
    def format_eta(sec: float) -> str:
        if sec < 60:
            return f"{int(sec)}с"
        return f"{int(sec // 60)}м {int(sec % 60)}с"

    @staticmethod
    def png_bytes_to_webp(raw_bytes: bytes, quality: int = 85, lossless: bool = False) -> bytes:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        if lossless:
            img.save(buf, format="WEBP", lossless=True)
        else:
            img.save(buf, format="WEBP", quality=quality, method=4)
        return buf.getvalue()
    
    @staticmethod
    def png_bytes_to_avif(raw_bytes: bytes, quality: int = 85) -> bytes:
        from PIL import Image
        import pillow_avif  # Активирует поддержку AVIF в Pillow
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="AVIF", quality=quality)
        return buf.getvalue()

    @staticmethod
    def png_bytes_to_png8(raw_bytes: bytes) -> bytes:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # Квантование (сжатие до 256 цветов) с идеальным сохранением прозрачности
        img_quantized = img.quantize(colors=256, method=2)
        buf = io.BytesIO()
        img_quantized.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    @staticmethod
    def estimate_quality_for_limit(image_files: List[Path], limit_bytes: int, lossless: bool = False, sample_count: int = 10) -> Tuple[int, bool]:
        if lossless or not image_files:
            return 85, False

        step = max(1, len(image_files) // sample_count)
        samples = image_files[::step][:sample_count]

        def avg_webp_size(q: int) -> int:
            total = 0
            for f in samples:
                raw = f.read_bytes()
                converted = ImageUtils.png_bytes_to_webp(raw, quality=q)
                total += len(converted)
            return int(total / len(samples) * len(image_files))

        lo, hi, best_q = 1, 95, 85
        for _ in range(8):
            mid = (lo + hi) // 2
            est = avg_webp_size(mid)
            json_est = int(est * (4 / 3))
            if json_est <= limit_bytes:
                best_q = mid
                lo = mid + 1
            else:
                hi = mid - 1
            if lo > hi:
                break

        return best_q, best_q < QUALITY_WARNING_THRESHOLD


class LottieProcessor:
    @staticmethod
    def fix_last_frame_op(lottie: dict) -> Tuple[bool, str]:
        global_op = lottie.get("op")
        if global_op is None:
            return False, "  Глобальный 'op' не найден."
        fixed, msgs = False, []
        for asset in lottie.get("assets", []):
            layers = asset.get("layers")
            if not layers:
                continue
            last = layers[-1]
            cur  = last.get("op")
            if cur is None:
                continue
            if cur != global_op:
                msgs.append(f"  [АВТОКОРРЕКЦИЯ] op {cur} → {global_op}")
                last["op"] = global_op
                fixed = True
            else:
                msgs.append(f"  [OK] op={cur} — корректно.")
        return fixed, "\n".join(msgs) if msgs else "  Sequence-слои не найдены."

    @staticmethod
    def embed_images(
            input_json: Path, images_dir: Path, output_json: Path,
            img_format: str = "webp", quality: int = 85, size_limit_mb: float = 0.0,
            log_fn: Callable[[str], None] = print,
            progress_fn: Optional[Callable[[int, int, float, float], None]] = None,
            cancel_check: Optional[Callable[[], bool]] = None
        ) -> Dict[str, Any]:
            
            if not input_json.exists(): raise FileNotFoundError(f"JSON не найден: {input_json}")
            if not images_dir.is_dir(): raise FileNotFoundError(f"Папка images/ не найдена: {images_dir}")

            log_fn(f"Читаю JSON: {input_json.name}...")
            with open(input_json, "r", encoding="utf-8") as f: lottie = json.load(f)

            was_fixed, fix_msg = LottieProcessor.fix_last_frame_op(lottie)
            if was_fixed: log_fn("  ✅ Автокоррекция 'op' применена.")

            assets = lottie.get("assets", [])
            work_items = []
            for i, asset in enumerate(assets):
                if "layers" in asset: continue
                p = asset.get("p", "")
                u = asset.get("u", "")
                if p.startswith("data:"): continue
                img = next((c for c in [images_dir / u / p, images_dir / p] if c.exists()), None)
                if img: work_items.append((i, asset, img))

            total = len(work_items)
            if total == 0: return {"path": output_json, "embedded": 0, "skipped": 0, "size_mb": 0}

            actual_quality = quality
            # Автолимит пока оставляем только для WebP (т.к. AVIF считается дольше)
            if img_format == "webp" and size_limit_mb > 0:
                limit_bytes = int(size_limit_mb * 1024 * 1024)
                log_fn(f"Подбираю качество под лимит {size_limit_mb} MB...")
                actual_quality, warn = ImageUtils.estimate_quality_for_limit([item[2] for item in work_items], limit_bytes, False)
                if warn: log_fn(f"  ⚠️ ВНИМАНИЕ: качество {actual_quality} ниже нормы.")

            log_fn(f"Режим: {img_format.upper()} | Изображений: {total}\n")

            def process_one(item: Tuple[int, dict, Path]) -> Tuple[int, str, int, int, str]:
                if cancel_check and cancel_check(): return item[0], "", 0, 0, item[2].name
                    
                idx, asset, img_path = item
                raw = img_path.read_bytes()
                
                # МАРШРУТИЗАТОР ФОРМАТОВ
                if img_format == "webp":
                    final_bytes = ImageUtils.png_bytes_to_webp(raw, quality=actual_quality, lossless=False)
                    mime = "image/webp"
                elif img_format == "lossless":
                    final_bytes = ImageUtils.png_bytes_to_webp(raw, quality=100, lossless=True)
                    mime = "image/webp"
                elif img_format == "avif":
                    final_bytes = ImageUtils.png_bytes_to_avif(raw, quality=actual_quality)
                    mime = "image/avif"
                elif img_format == "png8":
                    final_bytes = ImageUtils.png_bytes_to_png8(raw)
                    mime = "image/png"
                else:
                    mime = ImageUtils.get_mime_type(img_path)
                    final_bytes = raw
                    
                data_uri = f"data:{mime};base64,{base64.b64encode(final_bytes).decode()}"
                return idx, data_uri, len(raw), len(final_bytes), img_path.name

            workers = max(1, (os.cpu_count() or 4))
            results = {}
            done = total_orig = total_fin = 0
            start_time = time.time()

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(process_one, item): item for item in work_items}
                for future in as_completed(futures):
                    if cancel_check and cancel_check():
                        for f in futures: f.cancel() # Убиваем неначатые задачи
                        log_fn("  🛑 Конвертация прервана пользователем!")
                        break
                    
                    try:
                        idx, data_uri, orig_sz, fin_sz, name = future.result()
                        if not data_uri: 
                            # Если пустой результат пришёл из-за отмены внутри потока
                            if cancel_check and cancel_check():
                                break
                            continue
                    except Exception as e:
                        # ВОТ ЭТИ ДВЕ СТРОЧКИ ПОТЕРЯЛИСЬ В ПРОШЛЫЙ РАЗ:
                        log_fn(f"  ❌ Ошибка обработки кадра: {e}")
                        continue
                        
                    results[idx] = (data_uri, orig_sz, fin_sz)
                    done += 1
                    total_orig += orig_sz
                    total_fin += fin_sz
                    elapsed = time.time() - start_time
                    eta = (elapsed / done * (total - done)) if done else 0
                    est_mb = total_fin * (4 / 3) / (1 << 20) * (total / done)
                    
                    # Теперь мы показываем процент сжатия для всех новых форматов
                    if img_format in ("webp", "lossless", "avif", "png8"):
                        pct_saved = 100 - int(fin_sz / orig_sz * 100) if orig_sz else 0
                        log_fn(f"  [{done:>3}/{total}] {name:<35} -{pct_saved}%")
                    else:
                        log_fn(f"  [{done:>3}/{total}] {name:<35} {ImageUtils.format_size(orig_sz)}")
                    
                    if progress_fn: progress_fn(done, total, eta, est_mb)

            skipped = 0
            for i, asset in enumerate(assets):
                if i in results:
                    data_uri, _, _ = results[i]
                    asset["u"] = ""
                    asset["p"] = data_uri
                    asset["e"] = 1
                elif "layers" not in asset and not asset.get("p", "").startswith("data:"):
                    skipped += 1

            log_fn("\nСохраняю файл...")
            # Железобетонно создаем папку прямо перед записью
            output_json.parent.mkdir(parents=True, exist_ok=True) 
        
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(lottie, f, ensure_ascii=False, separators=(",", ":"))

            size_bytes = output_json.stat().st_size
            size_mb = size_bytes / (1 << 20)
            log_fn(f"  Итоговый JSON   : {ImageUtils.format_size(size_bytes)}")
            
            if progress_fn: progress_fn(total, total, 0, size_mb)
            return {"path": output_json, "embedded": len(results), "skipped": skipped, "size_mb": size_mb}


class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

class PreviewServerManager:
    def __init__(self):
        self.server = None
        self.thread = None
        self.lock = threading.Lock()

    def stop(self, log_fn: Optional[Callable] = None, wait_timeout: float = 1.0) -> None:
        def log(msg=""): (log_fn or dbg)(msg)
        with self.lock:
            srv = self.server
            thread = self.thread
            self.server = None
            self.thread = None

        if srv is None: return

        def _shutdown():
            try: srv.shutdown()
            except Exception: pass

        try:
            shutdown_thread = threading.Thread(target=_shutdown, daemon=True)
            shutdown_thread.start()
            shutdown_thread.join(timeout=wait_timeout)
            srv.server_close()
        except Exception: pass

    def _wait_for_port(self, port: int, timeout: float = 1.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    return True
            except OSError:
                time.sleep(0.03)
        return False

    def start(self, output_jsons: List[Path], log_fn: Optional[Callable] = None) -> None:
        def log(msg=""): (log_fn or dbg)(msg)
        
        if not output_jsons: return
        self.stop(log_fn=log, wait_timeout=0.5)

        # Получаем код библиотеки из assets.py
        try:
            lottie_js = get_lottie_js()
            if not lottie_js:
                log("  ⚠️ Библиотека lottie.js не найдена в assets.py, предпросмотр может не работать.")
        except Exception as e:
            log(f"  ❌ Ошибка загрузки assets: {e}")
            lottie_js = ""

        html_parts = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Lottie Preview Gallery</title>",
            "<script src='/lottie.js'></script>",
            "<style>",
            "  body{margin:0;background:#1e1e2e;font-family:sans-serif;color:#cdd6f4;display:flex;flex-wrap:wrap;gap:30px;padding:40px;padding-top:80px;justify-content:center;}",
            "  .top-bar{position:fixed;top:0;left:0;right:0;background:#181825;padding:12px;display:flex;justify-content:center;box-shadow:0 4px 10px rgba(0,0,0,0.5);z-index:100;}",
            "  .btn-bg{background:#7c6af7;color:#fff;border:none;padding:8px 20px;border-radius:6px;cursor:pointer;font-family:sans-serif;font-weight:bold;font-size:14px;transition:0.2s;}",
            "  .btn-bg:hover{background:#6a59e0;}",
            "  .card{background:#2a2a3e;padding:20px;border-radius:12px;box-shadow:0 10px 15px rgba(0,0,0,0.3);text-align:center;}",
            "  .anim-container{border-radius:8px;overflow:hidden;margin-bottom:12px;transition:background 0.3s;}",
            "  .bg-none{background:transparent;}",
            # --- ИЗМЕНЁННАЯ СТРОЧКА (СВЕТЛАЯ ШАХМАТКА) ---
            "  .bg-check{background-color:#ffffff;background-image:linear-gradient(45deg,#cccccc 25%,transparent 25%,transparent 75%,#cccccc 75%,#cccccc),linear-gradient(45deg,#cccccc 25%,transparent 25%,transparent 75%,#cccccc 75%,#cccccc);background-size:16px 16px;background-position:0 0,8px 8px;}",
            "  .bg-white{background:#ffffff;}",
            "  .bg-black{background:#000000;}",
            "  p{font-size:12px;opacity:0.7;margin:0;color:#a6e3a1;}",
            "</style></head><body>",
            
            # --- ПАНЕЛЬ И СКРИПТ КНОПКИ ---
            "  <div class='top-bar'><button id='btn-bg' class='btn-bg' onclick='toggleBg()'>Фон: Прозрачный</button></div>",
            "  <script>",
            "    let bgIdx = 0;",
            "    const bgs = ['bg-none', 'bg-check', 'bg-white', 'bg-black'];",
            "    const txt = ['Фон: Прозрачный', 'Фон: Шахматка', 'Фон: Белый', 'Фон: Черный'];",
            "    function toggleBg() {",
            "      const els = document.querySelectorAll('.anim-container');",
            "      els.forEach(el => el.classList.remove(bgs[bgIdx]));",
            "      bgIdx = (bgIdx + 1) % bgs.length;",
            "      els.forEach(el => el.classList.add(bgs[bgIdx]));",
            "      document.getElementById('btn-bg').innerText = txt[bgIdx];",
            "    }",
            "  </script>"
        ]

        paths_dict = {}
        for i, p in enumerate(output_jsons):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                w, h = data.get("w", 270), data.get("h", 270)
                
                # --- НОВОЕ 1: Ищем формат сжатия внутри JSON ---
                img_format = "Нет картинок"
                for asset in data.get("assets", []):
                    img_data = asset.get("p", "")
                    if isinstance(img_data, str) and img_data.startswith("data:image/"):
                        # Вырезаем 'data:image/' и ';' чтобы получить чистое название (WEBP, AVIF, PNG)
                        img_format = img_data.split(";")[0].replace("data:image/", "").upper()
                        break
            except Exception: 
                w, h, img_format = 270, 270, "Ошибка чтения"

            # --- НОВОЕ 2: Считаем реальный итоговый вес файла ---
            try:
                size_mb = p.stat().st_size / (1024 * 1024)
                size_str = f"{size_mb:.2f} MB"
            except Exception:
                size_str = "? MB"

            req_path = f"/data_{i}.json"
            paths_dict[req_path] = str(p)

            # --- Обновляем HTML карточки ---
            html_parts.append(f'''
            <div class="card">
                <div id="anim_{i}" class="anim-container bg-none" style="width:{w}px;height:{h}px"></div>
                <p style="line-height: 1.5;">
                    <strong>{p.name}</strong><br>
                    {w}×{h} px<br>
                    <span style="color:#f9e2af; font-size:11px; background:#181825; padding:2px 6px; border-radius:4px;">
                        Сжатие: {img_format} | Вес: {size_str}
                    </span>
                </p>
                <script>
                  window.addEventListener('load', function() {{
                    lottie.loadAnimation({{
                      container: document.getElementById('anim_{i}'),
                      renderer: 'svg', loop: true, autoplay: true, path: '{req_path}'
                    }});
                  }});
                </script>
            </div>
            ''')

        html_parts.append("</body></html>")
        full_html = "".join(html_parts)

        # Создаем обработчик, который "видит" наши переменные через замыкание
        class BatchHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path in ('/', '/test.html'):
                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(full_html.encode('utf-8'))
                elif self.path == '/lottie.js':
                    self.send_response(200)
                    self.send_header("Content-type", "application/javascript")
                    self.end_headers()
                    self.wfile.write(lottie_js.encode('utf-8'))
                elif self.path in paths_dict:
                    try:
                        with open(paths_dict[self.path], 'rb') as f:
                            content = f.read()
                        self.send_response(200)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(content)
                    except Exception: self.send_error(404)
                else: self.send_error(404)

        chosen_port = 8000
        for port in range(8000, 8010):
            try:
                srv = ReusableThreadingTCPServer(("127.0.0.1", port), BatchHandler)
                chosen_port = port
                break
            except OSError: continue
        else: return

        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        with self.lock:
            self.server, self.thread = srv, thread
        thread.start()

        url = f"http://localhost:{chosen_port}/test.html"
        log(f"  🌐 Предпросмотр запущен: {url}")
        webbrowser.open(url)


# ===========================================================================
# БЛОК 3 — Подготовка UI окружения и Tkinter
# ===========================================================================
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    print("[ОШИБКА] tkinter не найден.")
    sys.exit(1)

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    TkBase = TkinterDnD.Tk
    DND_AVAILABLE = True
except ImportError:
    TkBase = tk.Tk
    DND_FILES = None
    DND_AVAILABLE = False

def check_pillow() -> bool:
    try:
        importlib.import_module("PIL")
        return True
    except ImportError:
        return False

# ===========================================================================
# БЛОК 4 — Сплэш-Экран
# ===========================================================================
class SplashScreen(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lottie Embed Images — Проверка")
        self.configure(bg="#1e1e2e")
        self.resizable(False, False)
        
        W, H = 520, 360
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

        self.go_ahead = False
        self._build_ui()
        self.after(300, self._run_checks)

    def _build_ui(self):
        BG, BG_CARD, TEXT, SUBTEXT, ACCENT = "#1e1e2e", "#2a2a3e", "#cdd6f4", "#6e6a86", "#7c6af7"
        outer = tk.Frame(self, bg=BG, padx=32, pady=24)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Lottie Embed Images", bg=BG, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(outer, text="Проверка необходимых компонентов...", bg=BG, fg=SUBTEXT, font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 16))

        self.status_labels = {}
        for key, lbl_text in [("python", f"Python {sys.version_info.major}.{sys.version_info.minor}"), ("pillow", "Pillow (конвертация WebP)")]:
            row = tk.Frame(outer, bg=BG_CARD, padx=14, pady=10)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=lbl_text, bg=BG_CARD, fg=TEXT, font=("Segoe UI", 11), anchor="w").pack(side="left", fill="x", expand=True)
            sl = tk.Label(row, text="⏳ Проверяю...", bg=BG_CARD, fg=SUBTEXT, font=("Segoe UI", 10))
            sl.pack(side="right")
            self.status_labels[key] = sl

        self.log_text = tk.Text(outer, height=4, bg=BG, fg=SUBTEXT, font=("Consolas", 9), relief="flat", padx=8, pady=6, state="disabled", wrap="word")
        self.log_text.pack(fill="x", pady=(12, 0))

        self.pb_var = tk.DoubleVar(value=0)
        pb = ttk.Progressbar(outer, variable=self.pb_var, maximum=100)
        pb.pack(fill="x", pady=(8, 0))

        btn_frame = tk.Frame(outer, bg=BG)
        btn_frame.pack(fill="x", pady=(14, 0))
        self.btn_continue = tk.Button(btn_frame, text="▶  Продолжить", state="disabled", bg=ACCENT, fg="white", relief="flat", font=("Segoe UI", 11, "bold"), padx=20, pady=8, cursor="hand2", command=self._proceed)
        self.btn_continue.pack(side="right")

    def _log(self, msg: str):
        # Оборачиваем изменения UI в локальную функцию
        def update_ui():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        # Отправляем в очередь главного потока
        self.after(0, update_ui)

    def _set_status(self, key: str, ok: bool, text: str = ""):
        color = "#a6e3a1" if ok else "#f38ba8"
        symbol = "✅" if ok else "❌"
        # Отправляем в очередь главного потока (никаких update_idletasks в фоне!)
        def update_ui():
            self.status_labels[key].config(text=text or (symbol + " Установлен" if ok else symbol + " Не найден"), fg=color)
        self.after(0, update_ui)

    def _proceed(self):
        self.go_ahead = True
        self.destroy()

    def _run_checks(self):
    # Сплэш показывается ТОЛЬКО при запуске .py — в .exe он скипается в main().
    # Здесь дополнительно проверяем Pillow и при необходимости устанавливаем.
    # ВСЕ изменения UI — только через self.after(0, ...) — tkinter не потокобезопасен.

        def ui(fn):
            self.after(0, fn)

        def task():
            # Python — всегда OK если мы здесь (мы же запущены)
            ui(lambda: self._set_status(
                "python", True,
                f"✅  {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ))
            ui(lambda: self.pb_var.set(30))
            time.sleep(0.2)

            all_ok = True

            if check_pillow():
                # Pillow уже есть — просто отмечаем
                ui(lambda: self._set_status("pillow", True))
            else:
                # Pillow нет — пробуем установить через pip
                ui(lambda: self.status_labels["pillow"].config(
                    text="⏳ Устанавливаю...", fg="#f9e2af"
                ))
                ui(lambda: self._log("Pillow не найден. Устанавливаю автоматически..."))

                cmd = [sys.executable, "-m", "pip", "install", "Pillow"]
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True
                    )
                    for line in proc.stdout:
                        stripped = line.strip()
                        if stripped:
                            ui(lambda s=stripped: self._log(f"  {s}"))
                    proc.wait()

                    if proc.returncode == 0:
                        importlib.invalidate_caches()
                        ui(lambda: self._set_status("pillow", True))
                        ui(lambda: self._log("✅ Pillow успешно установлен."))
                    else:
                        raise RuntimeError("pip вернул ненулевой код")

                except Exception as e:
                    err = str(e)
                    all_ok = False
                    ui(lambda: self._set_status("pillow", False, "❌ Ошибка установки"))
                    ui(lambda: self._log(
                        f"❌ Не удалось установить автоматически.\n"
                        f"   Установи вручную: pip install Pillow\n"
                        f"   Причина: {err}"
                    ))

            ui(lambda: self.pb_var.set(100))

            if all_ok:
                ui(lambda: self._log("✅ Все компоненты готовы."))
                ui(lambda: self.after(1500, self._proceed))
                ui(lambda: self.btn_continue.config(
                    state="normal", text="▶  Продолжить"
                ))
            else:
                ui(lambda: self._log(
                    "⚠️  WebP-конвертация недоступна без Pillow.\n"
                    "    Можно продолжить — PNG-режим работает без Pillow."
                ))
                ui(lambda: self.btn_continue.config(
                    state="normal", text="Продолжить без WebP"
                ))

        threading.Thread(target=task, daemon=True).start()

# ===========================================================================
# БЛОК 5 — Основное GUI Приложение
# ===========================================================================
class LottieEmbedApp(TkBase):
    BG = "#1e1e2e"
    BG_CARD = "#2a2a3e"
    ACCENT = "#7c6af7"
    TEXT = "#cdd6f4"
    SUBTEXT = "#6e6a86"
    BORDER = "#3a3a5c"
    INPUT = "#313244"
    YELLOW = "#f9e2af"
    RED = "#f38ba8"
    GREEN = "#a6e3a1"

    def __init__(self):
        super().__init__()
        self.title("Lottie Embed Images")
        self.configure(bg=self.BG)
        
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        W, H = 1050, max(600, sh - max(90, int(sh * 0.08)))
        self.geometry(f"{W}x{H}+{max(0, (sw - W) // 2)}+10")
        self.minsize(1050, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.server_manager = PreviewServerManager()
        self.ui_queue = queue.Queue()
        self.worker_active = False

        self._est_cancel = False
        self._est_cancel_event = threading.Event()   # используется в _update_estimate
        self._cancel_conversion = threading.Event()  # потокобезопасный флаг отмены
        self._est_generation = 0
        self.render_queue = []

        self._init_vars()
        self._init_styles()
        self._build_ui()
        self._setup_dnd()

        self.after(100, self._show_startup_log)
        self.after(50, self._process_ui_queue)
        self.after(2000, self._check_for_updates) # <--- Наша проверка обновлений
    
    def _init_vars(self):
        self.var_json, self.var_images, self.var_output = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.var_format = tk.StringVar(value="webp")
        self.var_quality = tk.IntVar(value=85)
        self.var_use_limit = tk.BooleanVar(value=False)
        self.var_limit_mb = tk.DoubleVar(value=3.0)
        self.var_preview, self.var_open_folder = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.var_progress = tk.DoubleVar(value=0)
        
        # --- НОВЫЕ ПЕРЕМЕННЫЕ ДЛЯ УМНОГО UX ---
        self.var_eta = tk.StringVar()
        self.var_est_size = tk.StringVar(value="~ 0.00 MB")
        self.var_auto_quality = tk.StringVar()
        self.var_est_hint = tk.StringVar(value="Вставьте исходный JSON файл для начала расчётов.")
        self.is_estimation_active = False 
        # --------------------------------------
        
        self._cancel_conversion = threading.Event() # Это должен быть Event, а не False!
        self._est_cancel_event = threading.Event()
        
        self.var_json.trace_add("write", lambda *a: self._on_paths_changed())
        self.var_images.trace_add("write", lambda *a: self._on_paths_changed())

    def _on_paths_changed(self):
        self._update_run_button_text()
        self._update_preview_tab()

    def _init_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        
        for name, opts in {
            "TFrame": {"background": self.BG},
            "Card.TFrame": {"background": self.BG_CARD},
            "TLabel": {"background": self.BG, "foreground": self.TEXT, "font": ("Segoe UI", 10)},
            "Card.TLabel": {"background": self.BG_CARD, "foreground": self.TEXT, "font": ("Segoe UI", 10)},
            "Sub.TLabel": {"background": self.BG_CARD, "foreground": self.SUBTEXT, "font": ("Segoe UI", 9)},
            "Title.TLabel": {"background": self.BG, "foreground": self.TEXT, "font": ("Segoe UI", 15, "bold")},
            "Eta.TLabel": {"background": self.BG, "foreground": self.SUBTEXT, "font": ("Segoe UI", 9)},
            "TRadiobutton": {"background": self.BG_CARD, "foreground": self.TEXT, "font": ("Segoe UI", 10)},
            "HeaderSub.TLabel": {"background": self.BG, "foreground": self.SUBTEXT, "font": ("Segoe UI", 10)}, # <--- НОВОЕ
            "TCheckbutton": {"background": self.BG_CARD, "foreground": self.TEXT, "font": ("Segoe UI", 10)},
        }.items():
            style.configure(name, **opts)

        style.map("TRadiobutton", background=[("active", self.BG_CARD)])
        style.map("TCheckbutton", background=[("active", self.BG_CARD)])
        
        # Синяя кнопка
        style.configure("Run.TButton", background=self.ACCENT, foreground="white", font=("Segoe UI", 11, "bold"), padding=(12, 8), borderwidth=0)
        style.map("Run.TButton", background=[("active", "#6a59e0"), ("disabled", self.BORDER)], foreground=[("disabled", self.SUBTEXT)])
        
        # КРАСНАЯ КНОПКА СТОП
        style.configure("Stop.TButton", background=self.RED, foreground="white", font=("Segoe UI", 11, "bold"), padding=(12, 8), borderwidth=0)
        style.map("Stop.TButton", background=[("active", "#d9738e"), ("disabled", self.BORDER)], foreground=[("disabled", self.SUBTEXT)])
        
        style.configure("Add.TButton", 
                        background=self.BG_CARD, 
                        foreground=self.TEXT, 
                        font=("Segoe UI", 11), 
                        padding=(12, 8), 
                        borderwidth=0,               # <--- Убираем толщину рамки
                        lightcolor=self.BG_CARD,     # <--- Убиваем светлый блик
                        darkcolor=self.BG_CARD,      # <--- Убиваем тень
                        bordercolor=self.BG_CARD)    # <--- Сливаем цвет рамки с фоном
        style.map("Add.TButton", background=[("active", self.BORDER)])

        style.configure("custom.Horizontal.TProgressbar", troughcolor=self.BORDER, background=self.ACCENT, thickness=8, borderwidth=0)
        style.configure("TScale", background=self.BG_CARD, troughcolor=self.BORDER, sliderlength=16)

        style.configure("Vertical.TScrollbar", 
                        troughcolor=self.BG, 
                        background=self.BORDER, 
                        bordercolor=self.BG, 
                        arrowcolor=self.SUBTEXT, 
                        lightcolor=self.BORDER, # <--- Убиваем белые блики
                        darkcolor=self.BORDER,  # <--- Убиваем белые тени
                        relief="flat",
                        width=10,
                        arrowsize=10)
        style.map("Vertical.TScrollbar", background=[("active", "#5a5a7a")])

        # --- ВКЛАДКИ БЕЗ БЕЛЫХ РАМОК ---
        style.configure("TNotebook", 
                        background=self.BG, 
                        borderwidth=0, 
                        padding=0,
                        lightcolor=self.BG_CARD,    # <--- Сливаем с цветом контента
                        darkcolor=self.BG_CARD,     # <--- Сливаем с цветом контента
                        bordercolor=self.BG_CARD)   # <--- Убиваем главную обводку
                        
        style.configure("TNotebook.Tab", 
                        background=self.BG_CARD, 
                        foreground=self.SUBTEXT, 
                        padding=(16, 8), 
                        font=("Segoe UI", 10), 
                        borderwidth=0, 
                        focuscolor=self.BG_CARD,
                        lightcolor=self.BG_CARD, 
                        darkcolor=self.BG_CARD,
                        bordercolor=self.BG_CARD)   # <--- Убиваем рамку вокруг самих вкладок
                        
        style.map("TNotebook.Tab", 
                  background=[("selected", self.INPUT)], 
                  foreground=[("selected", self.TEXT)],
                  lightcolor=[("selected", self.INPUT)], 
                  darkcolor=[("selected", self.INPUT)],
                  bordercolor=[("selected", self.INPUT)], # <--- Убиваем рамку активной вкладки
                  expand=[("selected", [0, 0, 0, 0])])

    def _build_ui(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=5, minsize=400)
        outer.columnconfigure(1, weight=6, minsize=400)
        outer.rowconfigure(1, weight=1)

        hdr = ttk.Frame(outer)
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(hdr, text="Lottie Embed Images", style="Title.TLabel").pack(side="left")
        ttk.Label(hdr, text="   JSON + PNG-секвенция → один self-contained файл", style="HeaderSub.TLabel").pack(side="left", pady=(4, 0))

        # --- НОВАЯ КНОПКА ТЕМЫ В ШАПКЕ ---
        self.btn_theme = tk.Button(hdr, text="☀️ Светлая тема", font=("Segoe UI", 10, "bold"), 
                                   bg=self.BG, fg=self.YELLOW, bd=0, cursor="hand2",
                                   activebackground=self.BG, activeforeground=self.YELLOW,
                                   command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=(0, 5))
        # ---------------------------------

        left_outer = ttk.Frame(outer)
        left_outer.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(left_outer, bg=self.BG, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(left_outer, orient="vertical", command=self.canvas.yview, style="Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.left_panel = ttk.Frame(self.canvas, style="TFrame")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.left_panel, anchor="nw")
        
        self.left_panel.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))
        
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(2,0))
        self._bind_mousewheel(self.canvas)

        bottom_fixed = ttk.Frame(left_outer, style="TFrame")
        bottom_fixed.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self._build_settings_cards(self.left_panel)
        self._build_bottom_controls(bottom_fixed)
        self._build_right_panel(outer)

    def toggle_theme(self):
        if not hasattr(self, 'is_light_theme'):
            self.is_light_theme = False
            
        self.is_light_theme = not self.is_light_theme

        # 1. Сохраняем СТАРЫЕ цвета, включая акцентные (чтобы рекурсия их нашла)
        old_bg = self.BG
        old_bg_card = self.BG_CARD
        old_fg = self.TEXT
        old_subtext = self.SUBTEXT
        old_input = self.INPUT
        old_border = self.BORDER
        old_yellow = getattr(self, "YELLOW", "#f9e2af")
        old_green = getattr(self, "GREEN", "#a6e3a1")
        old_red = getattr(self, "RED", "#f38ba8")
        old_accent = getattr(self, "ACCENT", "#7c6af7")

        # 2. Переключаем глобальные цвета палитры
        if self.is_light_theme:
            # СВЕТЛАЯ ПАЛИТРА
            self.BG = "#eff1f5"          
            self.BG_CARD = "#e6e9ef"     
            self.TEXT = "#4c4f69"        
            self.SUBTEXT = "#8c8fa1"     
            self.INPUT = "#ccd0da"       
            self.BORDER = "#bcc0cc"      
            
            # Насыщенные акцентные цвета для светлого фона
            self.YELLOW = "#df8e1d"      # Глубокий желто-оранжевый
            self.GREEN = "#40a02b"       # Насыщенный зеленый
            self.RED = "#d20f39"         # Темно-красный
            self.ACCENT = "#1e66f5"      # Яркий синий
            
            self.btn_theme.config(text="🌙 Темная тема")
            
            # Цвета при наведении мышки (hover)
            active_btn_run = "#044ac2"
            active_btn_stop = "#b80d32"
            active_scroll = "#9ca0b0"
        else:
            # ТЕМНАЯ ПАЛИТРА
            self.BG = "#1e1e2e"
            self.BG_CARD = "#2a2a3e"
            self.TEXT = "#cdd6f4"
            self.SUBTEXT = "#6e6a86"
            self.INPUT = "#313244"
            self.BORDER = "#3a3a5c"
            
            # Пастельные акцентные цвета
            self.YELLOW = "#f9e2af"
            self.GREEN = "#a6e3a1"
            self.RED = "#f38ba8"
            self.ACCENT = "#7c6af7"
            
            self.btn_theme.config(text="☀️ Светлая тема")
            
            # Цвета при наведении мышки (hover)
            active_btn_run = "#6a59e0"
            active_btn_stop = "#d9738e"
            active_scroll = "#5a5a7a"

        # Обновляем кнопку переключения темы
        self.btn_theme.config(activebackground=self.BG, activeforeground=self.YELLOW)

        # 3. Обновляем стили TTK и Hover-эффекты (MAP)
        style = ttk.Style()
        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.BG_CARD)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT)
        style.configure("Card.TLabel", background=self.BG_CARD, foreground=self.TEXT)
        style.configure("Sub.TLabel", background=self.BG_CARD, foreground=self.SUBTEXT)
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT)
        style.configure("Eta.TLabel", background=self.BG, foreground=self.SUBTEXT)
        
        # --- ДОБАВЬ ВОТ ЭТИ ДВЕ НАСТРОЙКИ СЮДА ---
        style.configure("HeaderSub.TLabel", background=self.BG, foreground=self.SUBTEXT)
        style.configure("Vertical.TScrollbar", 
                        troughcolor=self.BG, 
                        background=self.BORDER, 
                        bordercolor=self.BG, 
                        arrowcolor=self.SUBTEXT, 
                        lightcolor=self.BORDER, 
                        darkcolor=self.BORDER)
        # ----------------------------------------
        
        style.configure("TRadiobutton", background=self.BG_CARD, foreground=self.TEXT)
        style.configure("TCheckbutton", background=self.BG_CARD, foreground=self.TEXT)
        
        # Исправляем цвет при наведении на чекбоксы и радио-кнопки
        style.map("TRadiobutton", background=[("active", self.BG_CARD)])
        style.map("TCheckbutton", background=[("active", self.BG_CARD)])
        style.map("Add.TButton", background=[("active", self.BORDER)])
        style.map("Vertical.TScrollbar", background=[("active", active_scroll)])

        # Кнопки с новыми акцентными цветами
        style.configure("Run.TButton", background=self.ACCENT, foreground="white")
        style.map("Run.TButton", background=[("active", active_btn_run), ("disabled", self.BORDER)], foreground=[("disabled", self.SUBTEXT)])
        
        style.configure("Stop.TButton", background=self.RED, foreground="white")
        style.map("Stop.TButton", background=[("active", active_btn_stop), ("disabled", self.BORDER)], foreground=[("disabled", self.SUBTEXT)])
        
        style.configure("custom.Horizontal.TProgressbar", background=self.ACCENT, troughcolor=self.BORDER)

        # Вкладки (исправляем выделение)
        style.configure("TNotebook", background=self.BG, lightcolor=self.BG_CARD, darkcolor=self.BG_CARD, bordercolor=self.BG_CARD)
        style.configure("TNotebook.Tab", background=self.BG_CARD, foreground=self.SUBTEXT, focuscolor=self.BG_CARD, lightcolor=self.BG_CARD, darkcolor=self.BG_CARD, bordercolor=self.BG_CARD)
        style.map("TNotebook.Tab", background=[("selected", self.INPUT)], foreground=[("selected", self.TEXT)], bordercolor=[("selected", self.INPUT)])

        style.configure("Add.TButton", background=self.BG_CARD, foreground=self.TEXT, lightcolor=self.BG_CARD, darkcolor=self.BG_CARD, bordercolor=self.BG_CARD)

        self.config(bg=self.BG)

        # 4. МАГИЯ РЕКУРСИИ
        def apply_new_colors(widget):
            try:
                current_bg = widget.cget("bg").lower()
                if current_bg == old_bg.lower(): widget.config(bg=self.BG)
                elif current_bg == old_bg_card.lower(): widget.config(bg=self.BG_CARD)
                elif current_bg == old_input.lower(): widget.config(bg=self.INPUT)
                elif current_bg == old_border.lower(): widget.config(bg=self.BORDER)
            except Exception: pass

            try:
                # ПРОВЕРЯЕМ И ПЕРЕКРАШИВАЕМ АКЦЕНТЫ (Зеленый статус, Желтый вес и т.д.)
                current_fg = widget.cget("fg").lower()
                if current_fg == old_fg.lower(): widget.config(fg=self.TEXT)
                elif current_fg == old_subtext.lower(): widget.config(fg=self.SUBTEXT)
                elif current_fg == old_yellow.lower(): widget.config(fg=self.YELLOW)
                elif current_fg == old_green.lower(): widget.config(fg=self.GREEN)
                elif current_fg == old_red.lower(): widget.config(fg=self.RED)
                elif current_fg == old_accent.lower(): widget.config(fg=self.ACCENT)
            except Exception: pass

            if isinstance(widget, (tk.Text, tk.Entry, tk.Canvas)):
                try:
                    if hasattr(widget, 'insertbackground'): widget.config(insertbackground=self.TEXT)
                    if hasattr(widget, 'force_redraw'): widget.force_redraw()
                except Exception: pass

            for child in widget.winfo_children():
                apply_new_colors(child)

        apply_new_colors(self)

    def _bind_mousewheel(self, widget):
        def _on_mousewheel(event):
            if sys.platform == "darwin": widget.yview_scroll(-1 * int(event.delta), "units")
            elif sys.platform == "win32": widget.yview_scroll(-1 * (event.delta // 120), "units")
        def _on_linux_scroll(event):
            widget.yview_scroll(-1 if event.num == 4 else 1, "units")
        
        self.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind_all("<Button-4>", _on_linux_scroll)
        self.bind_all("<Button-5>", _on_linux_scroll)

    def _card(self, parent, pady=(0, 8)):
        f = ttk.Frame(parent, style="Card.TFrame", padding=12)
        f.pack(fill="x", pady=pady)
        return f

    def _file_row(self, parent, var, cmd):
        r = ttk.Frame(parent, style="Card.TFrame")
        r.pack(fill="x", pady=(6, 0))
        # Ставим borderwidth=0 и highlightthickness=0 для полностью плоского дизайна
        e = tk.Entry(r, textvariable=var, bg=self.INPUT, fg=self.TEXT, insertbackground=self.TEXT, relief="flat", font=("Segoe UI", 10), borderwidth=0, highlightthickness=0)
        e.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        tk.Button(r, text="Обзор…", command=cmd, bg=self.BORDER, fg=self.TEXT, relief="flat", font=("Segoe UI", 9), padx=8, pady=4, cursor="hand2", borderwidth=0).pack(side="right")

    def _get_quality_color(self, val):
        # Если JSON не выбран, принудительно отдаем серый цвет (#3A3A5C или аналогичный для светлой)
        if not getattr(self, 'is_estimation_active', False):
            return self.BORDER
            
        is_light = getattr(self, 'is_light_theme', False)
            
        # Подбираем насыщенность в зависимости от темы
        if val >= 85: return "#3A9104" if is_light else "#93D22F"
        if val >= 65: return "#5CBA08" if is_light else "#B1E859"
        if val >= 50: return "#D9980B" if is_light else "#EFC44E"
        if val >= 40: return "#D36104" if is_light else "#F0913D"
        if val >= 30: return "#C22222" # Красный норм везде
        return "#860F0F"               # Темно-красный норм везде

    def _update_limit_slider_color(self, best_q):
        """Только меняет цвет и перерисовывает кастомный ползунок"""
        self.limit_slider_color = self._get_quality_color(best_q)
        if hasattr(self, 'limit_slider'):
            self.limit_slider.force_redraw()

    def _build_custom_slider(self, parent, variable, command, min_val=0, max_val=100, color_func=None, is_float=False):
        """Универсальный крутой ползунок с заливкой, сменой цвета и отключением"""
        canvas = tk.Canvas(parent, height=22, bg=self.BG_CARD, highlightthickness=0, borderwidth=0)

        def draw(*args):
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w <= 10: return
            val = variable.get()
            
            is_disabled = getattr(canvas, 'disabled', False)
            
            track_y1, track_y2 = h // 2 - 4, h // 2 + 4
            canvas.create_rectangle(0, track_y1, w, track_y2, fill=self.BG, outline=self.SUBTEXT, width=1)
            
            norm_val = (val - min_val) / (max_val - min_val) if max_val > min_val else 0
            norm_val = max(0, min(1, norm_val))
            fill_w = norm_val * w
            
            if is_disabled:
                color = self.BORDER # Серый цвет если отключено
            else:
                color = color_func(val) if color_func else "#93D22F"
            
            if fill_w > 0:
                canvas.create_rectangle(0, track_y1, fill_w, track_y2, fill=color, outline=self.SUBTEXT if not is_disabled else self.BG, width=1)
            
            thumb_w = 12
            thumb_x = max(thumb_w/2, min(fill_w, w - thumb_w/2))
            
            thumb_fill = "#e0e0e0" if not is_disabled else self.SUBTEXT
            canvas.create_rectangle(thumb_x - thumb_w/2, track_y1 - 2, thumb_x + thumb_w/2, track_y2 + 2, fill=thumb_fill, outline=self.BORDER)
            for offset in [-2, 0, 2]:
                canvas.create_line(thumb_x + offset, track_y1, thumb_x + offset, track_y2, fill="#888888" if not is_disabled else self.BORDER)

        def drag(event):
            if getattr(canvas, 'disabled', False): return
            w = canvas.winfo_width()
            if w <= 0: return
            x = max(0, min(event.x, w))
            norm_val = x / w
            val = min_val + norm_val * (max_val - min_val)
            val = max(min_val, min(max_val, val))
            
            if not is_float: val = int(val)
            else: val = round(val, 1)

            if variable.get() != val:
                variable.set(val)
                if command: command()
                
        canvas.bind("<Configure>", draw)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<Button-1>", drag)
        variable.trace_add("write", lambda *a: draw())
        
        canvas.force_redraw = draw
        return canvas

    def _build_settings_cards(self, parent):
        c1 = self._card(parent)
        ttk.Label(c1, text="📄  Исходные файлы или папка", style="Card.TLabel").pack(anchor="w")
        ttk.Label(c1, text="Выбери JSON(ы) или корневую папку с проектами", style="Sub.TLabel").pack(anchor="w", pady=(1,0))
        
        # --- Наша новая строка с двумя кнопками ---
        r = ttk.Frame(c1, style="Card.TFrame")
        r.pack(fill="x", pady=(6, 0))
        e = tk.Entry(r, textvariable=self.var_json, bg=self.INPUT, fg=self.TEXT, insertbackground=self.TEXT, relief="flat", font=("Segoe UI", 10), highlightthickness=1, highlightcolor=self.ACCENT, highlightbackground=self.BORDER)
        e.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        
        tk.Button(r, text="📁 Папка", command=self._browse_folder, bg=self.BORDER, fg=self.TEXT, relief="flat", font=("Segoe UI", 9), padx=8, pady=4, cursor="hand2").pack(side="right")
        tk.Button(r, text="📄 Файлы", command=self._browse_json, bg=self.BORDER, fg=self.TEXT, relief="flat", font=("Segoe UI", 9), padx=8, pady=4, cursor="hand2").pack(side="right", padx=(0, 4))
        # ------------------------------------------
        
        ttk.Label(c1, text="Можно перетащить файлы и папки в окно" if DND_AVAILABLE else "Drag & drop недоступен", style="Sub.TLabel").pack(anchor="w", pady=(6,0))

        c2 = self._card(parent)
        ttk.Label(c2, text="🖼  Папка с PNG-кадрами", style="Card.TLabel").pack(anchor="w")
        ttk.Label(c2, text="Папка images/ рядом с JSON", style="Sub.TLabel").pack(anchor="w", pady=(1,0))
        self._file_row(c2, self.var_images, lambda: self.var_images.set(filedialog.askdirectory(title="Папка images/")))

        self.c3 = self._card(parent)
        ttk.Label(self.c3, text="⚙️  Формат сжатия (Глобально)", style="Card.TLabel").pack(anchor="w", pady=(0, 6))
        
        for val, txt in [
            ("webp", "WebP (рекомендуется) — лучший баланс"), 
            ("avif", "AVIF (Next-Gen) — макс. сжатие (медленнее)"),
            ("png8", "PNG (Сжатый / PNG-8) — оптимизированный PNG"),
            ("lossless", "WebP Lossless — без потерь")
        ]:
            ttk.Radiobutton(self.c3, text=txt, variable=self.var_format, value=val, command=self._on_format_change).pack(anchor="w", pady=1)
        
        self.format_hint_label = tk.Label(self.c3, text="", bg=self.BG_CARD, fg=self.SUBTEXT, font=("Segoe UI", 9), wraplength=360, justify="left")
        self.format_hint_label.pack(anchor="w", pady=(6, 0))

        self.quality_frame = ttk.Frame(self.c3, style="Card.TFrame")
        self.quality_frame.pack(fill="x", pady=(8, 0))
        qrow = ttk.Frame(self.quality_frame, style="Card.TFrame")
        qrow.pack(fill="x")
        ttk.Label(qrow, text="Качество:", style="Card.TLabel").pack(side="left")
        self.lbl_qval = ttk.Label(qrow, text=str(self.var_quality.get()), style="Card.TLabel")
        self.lbl_qval.pack(side="left", padx=(5,0))
        ttk.Label(qrow, text="  (0 — меньше / 100 — лучше)", style="Sub.TLabel").pack(side="left")
        # Кастомный ползунок Качества
        self.quality_slider = self._build_custom_slider(
            self.quality_frame, 
            self.var_quality, 
            command=self._on_quality_change,
            min_val=0,
            max_val=100,
            color_func=self._get_quality_color, # Берем цвет напрямую от значения (0-100)
            is_float=False
        )
        self.quality_slider.pack(fill="x", pady=(4, 0))

        c_limit = self._card(parent)
        limit_hdr = ttk.Frame(c_limit, style="Card.TFrame")
        limit_hdr.pack(fill="x")
        ttk.Checkbutton(limit_hdr, text="📏  Ограничить размер JSON", variable=self.var_use_limit, command=self._on_limit_toggle).pack(side="left")
        self.lbl_limit_val = ttk.Label(limit_hdr, text=f"{self.var_limit_mb.get():.1f} MB", style="Card.TLabel")
        self.lbl_limit_val.pack(side="right")
        ttk.Label(c_limit, text="Скрипт подберёт качество автоматически", style="Sub.TLabel").pack(anchor="w", pady=(2,4))
        
        # Изначально делаем ползунок лимита серым (до загрузки файла)
        self.limit_slider_color = self.BORDER 
        self.limit_slider = self._build_custom_slider(c_limit, self.var_limit_mb, command=self._on_limit_change, min_val=0.5, max_val=10.0, color_func=lambda val: getattr(self, 'limit_slider_color', "#93D22F"), is_float=True)
        self.limit_slider.disabled = not self.var_use_limit.get()
        self.limit_slider.pack(fill="x", pady=(6,0))

        c_est = self._card(parent, pady=(0, 6))
        est_row = ttk.Frame(c_est, style="Card.TFrame")
        est_row.pack(fill="x")
        ttk.Label(est_row, text="📊  Оценка текущего файла:", style="Card.TLabel").pack(side="left")
        tk.Label(est_row, textvariable=self.var_est_size, bg=self.BG_CARD, fg=self.YELLOW, font=("Segoe UI", 10)).pack(side="left", padx=(6,0))
        
        # --- Наша новая динамическая подсказка ---
        tk.Label(c_est, textvariable=self.var_est_hint, bg=self.BG_CARD, fg=self.SUBTEXT, font=("Segoe UI", 9)).pack(anchor="w", pady=(2,0))
        
        tk.Label(c_est, textvariable=self.var_auto_quality, bg=self.BG_CARD, fg=self.YELLOW, font=("Segoe UI", 9)).pack(anchor="w", pady=(2,0))

        c4 = self._card(parent)
        ttk.Label(c4, text="💾  Итоговый JSON", style="Card.TLabel").pack(anchor="w")
        ttk.Label(c4, text="Подставляется автоматически (суффикс _result)", style="Sub.TLabel").pack(anchor="w", pady=(1,0))
        self._file_row(c4, self.var_output, lambda: self.var_output.set(filedialog.asksaveasfilename(defaultextension=".json")))

        c5 = self._card(parent, pady=(0, 8))
        ttk.Checkbutton(c5, text="🌐  Открыть предпросмотр после обработки", variable=self.var_preview).pack(anchor="w")
        ttk.Checkbutton(c5, text="📂  Открыть папку результата после обработки", variable=self.var_open_folder).pack(anchor="w")

    def _build_bottom_controls(self, parent):
        pb_frame = ttk.Frame(parent)
        pb_frame.pack(fill="x", pady=(0, 6))
        ttk.Progressbar(pb_frame, variable=self.var_progress, maximum=100, style="custom.Horizontal.TProgressbar").pack(fill="x")
        ttk.Label(pb_frame, textvariable=self.var_eta, style="Eta.TLabel").pack(anchor="e", pady=(3,0))

        btn_container = tk.Frame(parent, bg=self.BG)
        btn_container.pack(fill="x")
        
        self.btn_queue = ttk.Button(btn_container, text="➕ В очередь", style="Add.TButton", command=self._add_to_queue)
        self.btn_queue.pack(side="left", fill="x", expand=True, padx=(0, 4))
        
        self.btn_run = ttk.Button(btn_container, text="▶  Запустить", style="Run.TButton", command=self._run_conversion)
        self.btn_run.pack(side="right", fill="x", expand=True, padx=(4, 0))

    def _build_right_panel(self, parent):
        right = ttk.Frame(parent, style="TFrame")
        right.grid(row=1, column=1, sticky="nsew")
        
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        self.tab_info = tk.Frame(self.notebook, bg=self.BG_CARD, padx=10, pady=10)
        self.tab_preview = tk.Frame(self.notebook, bg=self.BG_CARD, padx=10, pady=10) # Наша новая скрытая вкладка
        self.tab_queue = tk.Frame(self.notebook, bg=self.BG_CARD)
        self.tab_log = tk.Frame(self.notebook, bg=self.BG_CARD, padx=10, pady=10)

        self.notebook.add(self.tab_info, text="📘 Инструкция")
        # Вкладку Обзор при старте НЕ добавляем (она скрыта)
        self.notebook.add(self.tab_queue, text="📑 Очередь (0)")
        self.notebook.add(self.tab_log, text="📝 Лог")

        self._build_tab_info()
        self._build_tab_preview()
        self._build_tab_queue()
        self._build_tab_log()

# ── Водяной знак (правый нижний угол) ─────────────────────────────────
        wm_frame = tk.Frame(self, bg=self.BG)
        wm_frame.place(relx=1.0, rely=1.0, anchor="se", x=-10, y=-12)

        tk.Label(wm_frame, text=f"{APP_BUILD}  ", bg=self.BG, fg=self.SUBTEXT, font=("Segoe UI", 8)).pack(side="left")

        wm_link = tk.Label(wm_frame, text="by griboed256", bg=self.BG, fg=self.ACCENT, font=("Segoe UI", 8, "underline"), cursor="hand2")
        wm_link.pack(side="left")
        wm_link.bind("<Button-1>", lambda e: webbrowser.open("https://t.me/griboed256"))

    def _build_tab_preview(self):
        """Создает элементы внутри вкладки предпросмотра"""
        self.lbl_preview_img = tk.Label(self.tab_preview, bg=self.BG_CARD)
        self.lbl_preview_img.pack(pady=(20, 16))

        self.lbl_preview_title = tk.Label(self.tab_preview, text="", bg=self.BG_CARD, fg=self.TEXT, font=("Segoe UI", 12, "bold"))
        self.lbl_preview_title.pack(anchor="center")

        self.lbl_preview_info = tk.Label(self.tab_preview, text="", bg=self.BG_CARD, fg=self.SUBTEXT, font=("Segoe UI", 10), justify="center")
        self.lbl_preview_info.pack(anchor="center", pady=(4, 16))

        self.lbl_preview_status = tk.Label(self.tab_preview, text="", bg=self.BG_CARD, font=("Segoe UI", 10, "bold"), justify="center", wraplength=350)
        self.lbl_preview_status.pack(anchor="center")
        
        # --- НОВЫЙ БЛОК: ТЕСТ КАЧЕСТВА ---
        self.quality_test_frame = tk.Frame(self.tab_preview, bg=self.BG_CARD, pady=10)
        self.quality_test_frame.pack(fill="x")
        
        tk.Label(self.quality_test_frame, text="🔍 Тест качества (1-й кадр при текущих настройках):", 
                 bg=self.BG_CARD, fg=self.YELLOW, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        
        self.lbl_quality_compare = tk.Label(self.quality_test_frame, bg=self.BG, 
                                            text="Передвиньте ползунок для теста", fg=self.SUBTEXT)
        self.lbl_quality_compare.pack(fill="x", pady=(5, 0))

    def _build_tab_info(self):
        tk.Label(self.tab_info, text="Инструкция", bg=self.BG_CARD, fg=self.SUBTEXT, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
        
        intro_label = tk.Label(
            self.tab_info,
            text=(
                "Это приложение преобразует Lottie-анимацию (экспорт из Bodymovin) с PNG-секвенцией в один self-contained JSON-файл, в который встроены все кадры анимации и могут сжаты в формат WebP для уменьшения итогового размера.\n"
                "Такой скомпелированный .json файл можно сразу передавать разработчикам — без папки images/.\n"
            ),
            bg=self.BG_CARD,
            fg=self.TEXT,
            justify="left",
            anchor="w",
            font=("Segoe UI", 10),
            wraplength=300
        )
        intro_label.pack(fill="x", pady=(0, 8))
        self.tab_info.bind("<Configure>", lambda e: intro_label.config(wraplength=max(260, self.tab_info.winfo_width() - 32)), add="+")

        self._create_accordion(self.tab_info, "📘", "КАК ПОЛЬЗОВАТЬСЯ", "1. Выбери исходный Lottie JSON (экспорт из Bodymovin).\nЕсли папка images/ лежит рядом — она подставится автоматически.\n\n2. Выбери формат изображений:\n• WebP (рекомендуется) — лучший баланс качества и размера\n• WebP Lossless — без потерь, но тяжелее\n• PNG — без конвертации\n\n3. (Опционально) включи «Ограничить размер JSON»\nи укажи лимит — качество подберётся автоматически.\n\n4. Нажми ▶ Запустить.\nВнизу отображается прогресс и оставшееся время.\n\n5. После завершения можно открыть предпросмотр:\nlocalhost:8000/test.html\nСервер закроется автоматически при выходе.")
        
        self._create_accordion(self.tab_info, "🗂️", "ПАКЕТНАЯ ОБРАБОТКА", "1. Выбери JSON-файл (или перетащи его в окно).\n2. Настрой сжатие слева (настройки будут общими для всей очереди).\n3. Нажми кнопку «➕ В очередь» внизу окна.\n4. Повтори эти шаги для всех нужных файлов.\n5. Перейди во вкладку «Очередь», чтобы проверить список (там можно удалить лишнее).\n6. Нажми «▶ Рендер очереди», чтобы обработать все файлы за один раз.")
        
        self._create_accordion(self.tab_info, "📦", "ПЕРЕДАЧА РАЗРАБОТЧИКАМ", "Передаётся только один файл:\n\n✅ *_result.json\n\nПапка images/ и исходный JSON не требуются.\nВсе кадры уже встроены внутрь итогового файла.")
        
        self._create_accordion(self.tab_info, "🎬", "ТРЕБОВАНИЯ К AFTER EFFECTS", "Композиция:\n• Размер = финальному размеру (270×270, 180×180, 90×90)\n• 30 fps\n• До ~300 кадров\n\nPNG-секвенция:\n• Импортирована как Image Sequence (не видео)\n• Размер PNG строго совпадает с композицией\n• Слой лежит прямо в основной композиции\n\nВажно:\nЕсли PNG больше композиции — ОБЯЗАТЕЛЬНО масштабируй в AE,\nиначе Bodymovin добавит трансформации и JSON станет тяжелее.\n\nИмя слоя:\n• Без .png в названии\n• Желательно с размером\n✅ YA_map-icon_anim_270x270")
        
        self._create_accordion(self.tab_info, "⚙️", "ЭКСПОРТ ИЗ BODYMOVIN", "Настройки:\n• Include in JSON — ❌ выключено\n• Copy to assets folder — ✅ включено\n\nПосле экспорта:\n• JSON файл\n• папка images/ с кадрами")
        
        self._create_accordion(self.tab_info, "✨", "ОСОБЕННОСТИ СКРИПТА", "• Автоматически исправляет параметр «op» последнего кадра\n  (частая ошибка Bodymovin)\n• Конвертирует изображения параллельно (быстро)\n• Встраивает всё в один JSON (base64)\n• Может сжимать в WebP для уменьшения веса\n\nЕсли в логе есть [АВТОКОРРЕКЦИЯ] — это нормально.")

    def _build_tab_queue(self):
        self.queue_canvas = tk.Canvas(self.tab_queue, bg=self.BG_CARD, highlightthickness=0, borderwidth=0)
        self.queue_scrollbar = ttk.Scrollbar(self.tab_queue, orient="vertical", command=self.queue_canvas.yview, style="Vertical.TScrollbar")
        self.queue_canvas.configure(yscrollcommand=self.queue_scrollbar.set)
        
        self.queue_inner = ttk.Frame(self.queue_canvas, style="Card.TFrame")
        self.queue_window = self.queue_canvas.create_window((0, 0), window=self.queue_inner, anchor="nw")
        
        self.queue_inner.bind("<Configure>", lambda e: self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all")))
        self.queue_canvas.bind("<Configure>", lambda e: self.queue_canvas.itemconfig(self.queue_window, width=e.width))
        
        self.queue_canvas.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.queue_scrollbar.pack(side="right", fill="y", padx=2)

        self.lbl_empty_queue = tk.Label(self.queue_canvas, text="Очередь пуста.\nНажми «➕ В очередь» слева.", bg=self.BG_CARD, fg=self.SUBTEXT, font=("Segoe UI", 11))
        self.lbl_empty_queue.place(relx=0.5, rely=0.5, anchor="center")

    def _build_tab_log(self):
        ttk.Label(self.tab_log, text="Лог выполнения", style="Sub.TLabel").pack(anchor="w", pady=(0,4))
        # Добавлены highlightthickness=0, borderwidth=0
        self.txt_log = tk.Text(self.tab_log, wrap="word", bg=self.INPUT, fg=self.TEXT, insertbackground=self.TEXT, font=("Consolas", 9), relief="flat", padx=10, pady=6, state="disabled", highlightthickness=0, borderwidth=0)
        self.txt_log.pack(side="left", fill="both", expand=True)
        
        scroll = ttk.Scrollbar(self.tab_log, command=self.txt_log.yview, style="Vertical.TScrollbar")
        scroll.pack(side="right", fill="y", padx=(2,0))
        self.txt_log["yscrollcommand"] = scroll.set

    def _create_accordion(self, parent, icon, title, content):
        container = tk.Frame(parent, bg=self.BG_CARD)
        container.pack(fill="x", pady=(6, 0))
        header = tk.Label(container, text=f"▶  {icon}  {title}", bg=self.INPUT, fg=self.TEXT, font=("Segoe UI", 10, "bold"), anchor="w", padx=12, pady=8, cursor="hand2")
        header.pack(fill="x")
        body_holder = tk.Frame(container, bg=self.BG_CARD, height=0)
        body_holder.pack(fill="x")
        body_holder.pack_propagate(False)
        body = tk.Label(body_holder, text=content, bg=self.BG_CARD, fg=self.TEXT, justify="left", anchor="nw", font=("Consolas", 9), padx=12, pady=9)
        body.pack(fill="x", anchor="nw")

        state = {"open": False, "target": 0, "current": 0}
        
        def update_wrap(e=None):
            body.config(wraplength=max(260, container.winfo_width() - 36))
            body.update_idletasks()
            if state["open"]:
                state["target"] = body.winfo_reqheight()
                state["current"] = state["target"]
                body_holder.configure(height=state["current"])

        container.bind("<Configure>", update_wrap)

        def toggle(e=None):
            state["open"] = not state["open"]
            header.config(text=f"{'▼' if state['open'] else '▶'}  {icon}  {title}")
            state["target"] = body.winfo_reqheight() if state["open"] else 0
            def animate():
                diff = state["target"] - state["current"]
                if abs(diff) > 2:
                    state["current"] += diff // 3
                    body_holder.configure(height=state["current"])
                    self.after(15, animate)
                else:
                    state["current"] = state["target"]
                    body_holder.configure(height=state["current"])
            animate()
            
        header.bind("<Button-1>", toggle)
        header.bind("<Enter>", lambda e: header.config(bg=self.BORDER))
        header.bind("<Leave>", lambda e: header.config(bg=self.INPUT))

    def _get_thumbnail(self, img_dir: Path) -> Any:
        try:
            pngs = list(img_dir.glob("*.png"))
            if not pngs: return None
            from PIL import Image, ImageTk
            img = Image.open(pngs[0])
            img.thumbnail((50, 50))
            return ImageTk.PhotoImage(img)
        except Exception:
            return None
        
    def _stop_preview_animation(self):
        """Останавливает анимацию и жестко чистит оперативную память от старых кадров"""
        if hasattr(self, '_preview_anim_job') and self._preview_anim_job:
            self.after_cancel(self._preview_anim_job)
            self._preview_anim_job = None
            
        # Защита от утечки памяти (Memory Leak)
        if hasattr(self, '_preview_frames'):
            self._preview_frames.clear() # Жестко удаляем старые кадры из RAM

    def _animate_preview(self):
        """Перелистывает кадры как в блокноте (flipbook)"""
        if not hasattr(self, '_preview_frames') or not self._preview_frames:
            return
        
        self._preview_frame_idx = (self._preview_frame_idx + 1) % len(self._preview_frames)
        self.lbl_preview_img.config(image=self._preview_frames[self._preview_frame_idx])
        
        
        self._preview_anim_job = self.after(33, self._animate_preview)
        
    def _update_preview_tab(self):
        """Анализирует JSON и показывает вкладку 'Обзор файла' с анимацией"""
        if not hasattr(self, 'notebook'):
            return

        jp = self.var_json.get().strip()
        imd = self.var_images.get().strip()

        tabs = self.notebook.tabs()
        tab_id = str(self.tab_preview)

        # Сначала сбрасываем изображение в label — это освобождает ссылку Tkinter на PhotoImage.
        # Без этого .clear() не помогает — label держит объект в памяти.
        self.lbl_preview_img.config(image="")
        self._stop_preview_animation()
        self._preview_frames = []

        # Прячем вкладку, если поля пусты или файл - не json
        if not jp or not jp.lower().endswith('.json') or not Path(jp).is_file():
            if tab_id in tabs:
                self.notebook.forget(self.tab_preview)
            return

        json_path = Path(jp)
        img_dir = Path(imd) if imd else None

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Вытаскиваем метаданные
            w = data.get("w", "?")
            h = data.get("h", "?")
            op = data.get("op", 0)
            ip = data.get("ip", 0)
            frames_count = int(op - ip)

            # Ищем недостающие файлы и проверяем на видео
            # Ищем недостающие файлы и анализируем "внутрянку" на ошибки
            assets = data.get("assets", [])
            missing_images = []
            
            # Наши новые флаги-предохранители
            has_video = False
            has_audio = False
            has_jpg = False
            is_already_embedded = False
            embedded_count = 0
            image_count = 0

            for asset in assets:
                if "layers" in asset: continue
                
                img_id = str(asset.get("id", "")).lower()
                p = str(asset.get("p", ""))
                p_lower = p.lower()
                u = str(asset.get("u", ""))
                
                # --- ПРОВЕРКИ ---
                if img_id.startswith("video"):
                    has_video = True
                
                if img_id.startswith("audio") or p_lower.endswith((".mp3", ".wav")):
                    has_audio = True
                    
                if p_lower.endswith((".jpg", ".jpeg")):
                    has_jpg = True

                if p.startswith("data:image/"):
                    embedded_count += 1
                    continue # Этот файл уже встроен, пропускаем поиск на диске

                image_count += 1

                # Поиск файлов на диске (если они не встроены)
                if img_dir and img_dir.is_dir():
                    if not (img_dir / u / p).exists() and not (img_dir / p).exists():
                        missing_images.append(p)
                else:
                    missing_images.append(p)

            # Если все найденные картинки уже в base64 — значит галку не сняли
            if image_count == 0 and embedded_count > 0:
                is_already_embedded = True

            # Обновляем тексты
            self.lbl_preview_title.config(text=json_path.name)
            self.lbl_preview_info.config(text=f"Размер: {w} × {h} px  |  Длительность: {frames_count} кадров. \n\n p.s предпросмотр может не корректно отображать FPS анимации.\n Корректно анимация отображается только в браузере в конце рендера.\n")

            # --- УМНАЯ СИСТЕМА ВЫВОДА СТАТУСОВ (ОТ КРИТИЧНЫХ К МЕЛКИМ) ---
            if has_video:
                err_text = "❌ Ошибка: Обнаружен видео-файл (не PNG-секвенция)!\nУдалите видео из композиции AE и перерендерите в секвенцию."
                self.lbl_preview_status.config(text=err_text, fg=self.RED)
                self.lbl_preview_img.config(image="", text="🎞️", font=("Segoe UI", 40))
                
            elif has_audio:
                err_text = "❌ Ошибка: В проекте остался аудио-файл!\nЗвук вызывает ошибки в Lottie-плеерах. Удалите аудиослой из AE."
                self.lbl_preview_status.config(text=err_text, fg=self.RED)
                self.lbl_preview_img.config(image="", text="🔊", font=("Segoe UI", 40))
                
            elif is_already_embedded:
                err_text = "⚠️ Внимание: Картинки уже встроены (Base64)!\nВ Bodymovin была включена опция «Include in JSON». Наш скрипт не сможет их сжать."
                self.lbl_preview_status.config(text=err_text, fg=self.YELLOW)
                self.lbl_preview_img.config(image="", text="📦", font=("Segoe UI", 40))
                
            elif has_jpg:
                err_text = "⚠️ Внимание: Секвенция отрендерена в JPG!\nJPG не поддерживает прозрачность фона. Рекомендуется использовать PNG."
                self.lbl_preview_status.config(text=err_text, fg=self.YELLOW)
                self.lbl_preview_img.config(image="", text="🖼️", font=("Segoe UI", 40))
                
            elif missing_images:
                err_text = f"❌ Ошибка: Не найдены кадры в папке images/\nНапример: {missing_images[0]}"
                self.lbl_preview_status.config(text=err_text, fg=self.RED)
                self.lbl_preview_img.config(image="", text="🖼️", font=("Segoe UI", 40))
                
            elif w != "?" and (int(w) > 1000 or int(h) > 1000):
                err_text = "⚠️ Предупреждение: Очень большое разрешение композиции!\n(> 1000px). Анимация может сильно тормозить на смартфонах."
                self.lbl_preview_status.config(text=err_text, fg=self.YELLOW)
                self.lbl_preview_img.config(image="", text="⏳ Загрузка...", font=("Segoe UI", 12))
                self._start_preview_loading(img_dir) # Запускаем загрузку превью
                
            else:
                self.lbl_preview_status.config(text="✅ Все кадры найдены, готов к рендеру!", fg=self.GREEN)
                self.lbl_preview_img.config(image="", text="⏳ Загрузка анимации...", font=("Segoe UI", 12))
                self._start_preview_loading(img_dir) # Запускаем загрузку превью

            # Показываем вкладку
            if tab_id not in tabs:
                self.notebook.insert(1, self.tab_preview, text="👁️ Обзор файла")
                self.notebook.select(self.tab_preview)

        except Exception as e:
            # Если произошла ошибка парсинга - прячем вкладку
            if tab_id in tabs:
                self.notebook.forget(self.tab_preview)
                
                # --- МАГИЯ АНИМАЦИИ ---
                # Запускаем загрузку кадров в фоновом потоке, чтобы интерфейс не завис
                if img_dir and img_dir.is_dir():
                    def load_animation_frames():
                        import re
                        from PIL import Image, ImageTk
                        
                        # Умная сортировка: чтобы 2.png шло перед 10.png
                        def natural_sort_key(s):
                            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s.name)]
                        
                        pngs = sorted(img_dir.glob("*.png"), key=natural_sort_key)
                        pil_images = []
                        
                        # Читаем и сжимаем все картинки
                        for p in pngs:
                            try:
                                img = Image.open(p)
                                img.thumbnail((300, 300))
                                pil_images.append(img)
                            except: pass
                            
                        # Передаем готовые кадры обратно в главный интерфейс
                        def update_ui():
                            self._preview_frames = [ImageTk.PhotoImage(img) for img in pil_images]
                            if self._preview_frames:
                                self._preview_frame_idx = 0
                                self.lbl_preview_img.config(image=self._preview_frames[0], text="")
                                # Запускаем моторчик анимации, если кадров больше одного!
                                if len(self._preview_frames) > 1:
                                    self._preview_anim_job = self.after(33, self._animate_preview)
                            else:
                                self.lbl_preview_img.config(image="", text="🖼️", font=("Segoe UI", 40))
                                
                        self.after(0, update_ui)
                        
                    threading.Thread(target=load_animation_frames, daemon=True).start()

            # Показываем вкладку
            if tab_id not in tabs:
                self.notebook.insert(1, self.tab_preview, text="👁️ Обзор файла")
                self.notebook.select(self.tab_preview)

        except Exception:
            if tab_id in tabs:
                self.notebook.forget(self.tab_preview)
                
    def _start_preview_loading(self, img_dir: Path):
        """Асинхронно загружает кадры для превью-анимации"""
        if not img_dir or not img_dir.is_dir():
            return
            
        def load_animation_frames():
            import re
            from PIL import Image, ImageTk
            
            # Умная сортировка: чтобы 2.png шло перед 10.png
            def natural_sort_key(s):
                return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s.name)]
            
            pngs = sorted(img_dir.glob("*.png"), key=natural_sort_key)
            pil_images = []
            
            # Читаем и сжимаем все картинки
            for p in pngs:
                try:
                    img = Image.open(p)
                    img.thumbnail((300, 300))
                    pil_images.append(img)
                except: pass
                
            # Передаем готовые кадры обратно в главный интерфейс
            def update_ui():
                self._preview_frames = [ImageTk.PhotoImage(img) for img in pil_images]
                if self._preview_frames:
                    self._preview_frame_idx = 0
                    self.lbl_preview_img.config(image=self._preview_frames[0], text="")
                    # Запускаем моторчик анимации, если кадров больше одного!
                    if len(self._preview_frames) > 1:
                        self._preview_anim_job = self.after(33, self._animate_preview)
                else:
                    self.lbl_preview_img.config(image="", text="🖼️", font=("Segoe UI", 40))
                    
            self.after(0, update_ui)
            
        threading.Thread(target=load_animation_frames, daemon=True).start()

    def _add_direct_to_queue(self, json_path: Path, custom_out_dir: Optional[Path] = None) -> bool:
        """Тихо добавляет файл в очередь при пакетном перетаскивании"""
        if any(item['json_path'] == json_path for item in self.render_queue):
            return False

        img_dir = json_path.parent / "images"
        
        # --- НОВАЯ ЛОГИКА СОХРАНЕНИЯ ---
        if custom_out_dir:
            out_path = custom_out_dir / f"{json_path.stem}_result.json"
        else:
            out_path = json_path.parent / f"{json_path.stem}_result.json"

        photo = self._get_thumbnail(img_dir)

        queue_item = {
            "id": f"{time.time()}_{json_path.name}",
            "json_path": json_path,
            "img_dir": img_dir,
            "out_path": out_path,
            "thumb_img": photo,
            "est_size": "Из папки"
        }
        
        self.render_queue.append(queue_item)
        self._build_queue_card(queue_item)
        self._update_queue_ui()
        return True

    def _validate_lottie_json(self, file_path: Path) -> bool:
        """
        Проверяет, является ли файл корректной Lottie-анимацией с PNG-секвенцией.
        Проверки: обязательные поля, непустой layers, наличие image-ассетов.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 1. Обязательные поля (layers добавлен к прошлой версии)
            required_keys = {"v", "fr", "ip", "op", "layers", "assets"}
            if not required_keys.issubset(data.keys()):
                return False

            # 2. layers не должен быть пустым
            if not data.get("layers"):
                return False

            # 3. В assets должен быть хотя бы один image-ассет
            #    (не вложенная композиция, не уже встроенный data-uri)
            assets = data.get("assets", [])
            has_images = any(
                "layers" not in a and not a.get("p", "").startswith("data:")
                for a in assets
            )
            return has_images

        except Exception:
            return False

    def _setup_dnd(self):
        if not DND_AVAILABLE: return
        
        # 1. Создаем красивый "экран загрузки" (оверлей), который изначально скрыт
        self.dnd_overlay = tk.Frame(self, bg=self.ACCENT)
        lbl = tk.Label(self.dnd_overlay, text="📂 Отпустите файлы или папки здесь", bg=self.ACCENT, fg="white", font=("Segoe UI", 24, "bold"))
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        
        # 2. Регистрируем главное окно и наш новый оверлей для приема файлов
        self.drop_target_register(DND_FILES)
        self.dnd_overlay.drop_target_register(DND_FILES)
        
        # 3. Логика появления и исчезновения
        def show_overlay(e):
            # Растягиваем оверлей на всё окно и выводим на передний план
            self.dnd_overlay.place(x=0, y=0, relwidth=1, relheight=1)
            self.dnd_overlay.tkraise()
            
        def hide_overlay(e):
            # Прячем оверлей
            self.dnd_overlay.place_forget()
            
        def on_drop(e):
            # При сбрасывании файла: прячем экран и передаем данные обработчику
            hide_overlay(e)
            self._apply_json_path(e.data)

        # 4. Привязываем события (Enter - зашли в окно, Leave - вышли, Drop - бросили)
        self.dnd_bind("<<DropEnter>>", show_overlay)
        self.dnd_overlay.dnd_bind("<<DropLeave>>", hide_overlay)
        
        self.dnd_bind("<<Drop>>", on_drop)
        self.dnd_overlay.dnd_bind("<<Drop>>", on_drop)

    def _apply_json_path(self, raw_data: str):
        if not raw_data: return
        
        try:
            paths = self.tk.splitlist(raw_data)
        except Exception:
            p = raw_data.strip().strip('"{}')
            if "} {" in p: p = p.split("} {")[0].strip("{}")
            paths = [p]

        import os
        valid_paths = [Path(p).resolve() for p in paths if Path(p).exists()]
        if not valid_paths: return

        # Определяем "корень" — общую папку для всех перетаскиваемых элементов
        common_path = Path(os.path.commonpath([str(p.resolve()) for p in valid_paths]))
        if common_path.is_file():
            common_path = common_path.parent

        # 1. Режим "Соло" (ровно один .json файл)
        if len(valid_paths) == 1 and valid_paths[0].is_file() and valid_paths[0].suffix.lower() == ".json":
            path = valid_paths[0]
            if path.name.endswith("_result.json"):
                messagebox.showwarning("Внимание", "Это уже обработанный файл (_result.json).")
                return
            if not self._validate_lottie_json(path):
                messagebox.showwarning("Ошибка", "Кажется, это не Lottie-анимация. Проверьте файл.")
                return

            self.var_json.set(str(path))
            img_dir = path.parent / "images"
            self.var_images.set(str(img_dir) if img_dir.exists() else "")
            self.var_output.set(str(path.parent / f"{path.stem}_result.json"))
            self._update_estimate()
            return

        # 2. Режим "Супер Очередь" (папки или несколько файлов)
        results_dir = common_path / "results-convert"
        
        found_jsons = []
        for p in valid_paths:
            if p.is_file() and p.suffix.lower() == ".json":
                found_jsons.append(p)
            elif p.is_dir():
                for json_file in p.rglob("*.json"):
                    found_jsons.append(json_file)

        added_count = 0
        for jp in found_jsons:
            # Защита 1: Пропускаем уже готовые результаты
            if jp.name.endswith("_result.json"): continue
            # Защита 2: НЕ ИЩЕМ внутри нашей новой папки (чтобы не зациклиться)
            if "results-convert" in jp.parts: continue
            # Защита 3: Проверяем, что это правильная Lottie-анимация
            if not self._validate_lottie_json(jp): continue
                
            # Передаем кастомную папку для сохранения
            if self._add_direct_to_queue(jp, custom_out_dir=results_dir):
                added_count += 1

        if added_count > 0:
            # Физически создаем эту папку на компьютере
            results_dir.mkdir(exist_ok=True)
            self.notebook.select(self.tab_queue)

            self.var_json.set("")
            self.var_images.set("")
            self.var_output.set("")
            self.var_est_size.set("")
            
            self._log_msg(f"✅ Умный поиск: найдено {added_count} файлов.\n📂 Сохранение в: {results_dir.name}\n")
        else:
            messagebox.showinfo("Поиск", "Не найдено подходящих новых Lottie JSON файлов.")

    def _browse_json(self):
        paths = filedialog.askopenfilenames(title="Выбери исходные JSON файлы", filetypes=[("JSON", "*.json")])
        if paths:
            raw_data = " ".join(f"{{{p}}}" for p in paths)
            self._apply_json_path(raw_data)

    def _browse_folder(self):
        """Открывает диалог выбора папки и передает её умному сканеру"""
        p = filedialog.askdirectory(title="Выбери корневую папку с проектами")
        if p: 
            self._apply_json_path(p)

    def _add_to_queue(self):
        jp = self.var_json.get().strip()
        imd = self.var_images.get().strip()
        outp = self.var_output.get().strip()

        if not jp or not imd or not outp:
            messagebox.showwarning("Ошибка", "Заполни все пути перед добавлением в очередь.")
            return

        json_path = Path(jp)
        if not json_path.exists():
            messagebox.showerror("Ошибка", "Файл JSON не существует.")
            return

        if not self._validate_lottie_json(json_path):
            messagebox.showwarning("Ошибка", "Кажется, это не Lottie-анимация. Проверьте файл.")
            return

        if any(item['json_path'] == json_path for item in self.render_queue):
            messagebox.showinfo("Инфо", "Этот файл уже есть в очереди.")
            return

        photo = self._get_thumbnail(Path(imd))
        
        queue_item = {
            "id": time.time(),
            "json_path": json_path,
            "img_dir": Path(imd),
            "out_path": Path(outp),
            "thumb_img": photo,
            "est_size": self.var_est_size.get() or "Неизвестно"
        }
        
        self.render_queue.append(queue_item)
        self._build_queue_card(queue_item)
        self._update_queue_ui()
        
        self.notebook.select(self.tab_queue)
        self.var_json.set("")
        self.var_images.set("")
        self.var_output.set("")
        self.var_est_size.set("")

    def _build_queue_card(self, item: dict):
        card = tk.Frame(self.queue_inner, bg=self.INPUT, highlightthickness=1, highlightbackground=self.BORDER)
        card.pack(fill="x", pady=4, padx=4)
        item["widget"] = card

        if item["thumb_img"]:
            lbl_img = tk.Label(card, image=item["thumb_img"], bg=self.INPUT)
            lbl_img.pack(side="left", padx=8, pady=8)
        else:
            lbl_img = tk.Label(card, text="JSON", bg=self.BORDER, fg=self.TEXT, width=6, height=3)
            lbl_img.pack(side="left", padx=8, pady=8)

        info = tk.Frame(card, bg=self.INPUT)
        info.pack(side="left", fill="both", expand=True, pady=8)
        
        tk.Label(info, text=item["json_path"].name, bg=self.INPUT, fg=self.TEXT, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(info, text=f"Вес: {item['est_size']}", bg=self.INPUT, fg=self.SUBTEXT, font=("Segoe UI", 9), anchor="w").pack(fill="x")

        btn_del = tk.Button(card, text="✖", bg=self.INPUT, fg=self.RED, font=("Segoe UI", 10), relief="flat", cursor="hand2", command=lambda: self._remove_from_queue(item))
        btn_del.pack(side="right", padx=12)

    def _remove_from_queue(self, item_to_remove: dict):
        self.render_queue = [item for item in self.render_queue if item["id"] != item_to_remove["id"]]
        if "widget" in item_to_remove:
            item_to_remove["widget"].destroy()
        self._update_queue_ui()

    def _update_queue_ui(self):
        count = len(self.render_queue)
        self.notebook.tab(self.tab_queue, text=f"📑 Очередь ({count})")
        if count == 0:
            self.lbl_empty_queue.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.lbl_empty_queue.place_forget()
        self._update_run_button_text()

    def _update_run_button_text(self):
        if not hasattr(self, 'btn_run'):
            return
        json_filled = bool(self.var_json.get().strip())
        queue_count = len(self.render_queue)

        if json_filled:
            self.btn_run.config(text="▶  Запустить файл")
        elif queue_count > 0:
            self.btn_run.config(text=f"▶  Рендер очереди ({queue_count})")
        else:
            self.btn_run.config(text="▶  Запустить")

    def _on_format_change(self):
        fmt = self.var_format.get()
        
        # Умные пресеты качества при переключении
        if fmt == "avif":
            self.var_quality.set(60)
            self.lbl_qval.config(text="60")
        elif fmt == "webp":
            # Возвращаем стандарт индустрии для WebP
            self.var_quality.set(85)
            self.lbl_qval.config(text="85")

        # Показываем/скрываем ползунок качества
        if fmt in ("webp", "avif"):
            self.quality_frame.pack(fill="x", pady=(8, 0))
        else:
            self.quality_frame.pack_forget()
            
        self._update_estimate()

    def _on_quality_change(self):
        self.lbl_qval.config(text=str(self.var_quality.get()))
        if hasattr(self, 'var_use_limit') and not self.var_use_limit.get(): 
            self._update_estimate()

    def _on_limit_toggle(self):
        is_on = self.var_use_limit.get()
        
        # 1. Включаем/выключаем ползунок лимита
        if hasattr(self, 'limit_slider'):
            self.limit_slider.disabled = not is_on
            self.limit_slider.force_redraw()
            
        # --- НОВОЕ: Отключаем ползунок качества, если включен автолимит ---
        if hasattr(self, 'quality_slider'):
            self.quality_slider.disabled = is_on
            self.quality_slider.force_redraw()
        # -----------------------------------------------------------------
            
        # 2. Блокируем радиокнопки форматов
        radio_state = "disabled" if is_on else "normal"
        for child in self.c3.winfo_children():
            if isinstance(child, ttk.Radiobutton):
                child.configure(state=radio_state)

        self.lbl_limit_val.config(state="normal" if is_on else "disabled")
        
        if is_on:
            self.var_format.set("webp")
            self.quality_frame.pack(fill="x", pady=(8, 0))
            # Чуть-чуть обновил текст подсказки, чтобы было понятнее
            self.format_hint_label.config(text="Формат и качество заблокированы (автоподбор под лимит).")
        else:
            self.format_hint_label.config(text="")
            self._on_format_change()
            
        self._update_estimate()

    def _on_limit_change(self):
        self.lbl_limit_val.config(text=f"{self.var_limit_mb.get():.1f} MB")
        self._update_estimate()

    def _update_estimate(self):
        # Отменяем предыдущий запланированный запуск, если он был
        if hasattr(self, '_est_job_id') and self._est_job_id:
            self.after_cancel(self._est_job_id)
        
        # Планируем новый запуск через 1000мс (1 секунда)
        self._est_job_id = self.after(1000, self._do_update_estimate_task)

    def _do_update_estimate_task(self):
        """Реальный расчет веса и создание превью качества"""
        img_dir_str = self.var_images.get().strip()
        fmt, quality = self.var_format.get(), self.var_quality.get()
        limit = self.var_limit_mb.get() if self.var_use_limit.get() else 0
        
        if not img_dir_str or not Path(img_dir_str).is_dir():
            return

        pngs = sorted(Path(img_dir_str).glob("*.png"))
        if not pngs: return

        self.is_estimation_active = True
        self._est_generation += 1
        gen = self._est_generation
        self.var_est_size.set("⏳ Считаю...")
        self.lbl_quality_compare.config(text="⏳ Сжимаю тестовый кадр...")

        def _task():
            try:
                from PIL import Image, ImageTk
                # Берем первый кадр для теста качества
                test_img_path = pngs[0]
                raw_bytes = test_img_path.read_bytes()
                
                # Выбираем функцию сжатия
                if fmt == "avif":
                    conv_func = lambda b: ImageUtils.png_bytes_to_avif(b, quality=quality)
                elif fmt == "png8":
                    conv_func = lambda b: ImageUtils.png_bytes_to_png8(b)
                elif fmt == "lossless":
                    conv_func = lambda b: ImageUtils.png_bytes_to_webp(b, quality=100, lossless=True)
                else: # webp
                    conv_func = lambda b: ImageUtils.png_bytes_to_webp(b, quality=quality)

                # 1. Генерируем тестовую картинку (сжатую)
                compressed_bytes = conv_func(raw_bytes)
                test_img = Image.open(io.BytesIO(compressed_bytes))
                test_img.thumbnail((350, 350)) # Размер чуть больше обычного превью
                tk_img = ImageTk.PhotoImage(test_img)

                # 2. Считаем средний вес (на 8 семплах)
                samples = pngs[::max(1, len(pngs) // 8)][:8]
                total_final = sum(len(conv_func(p.read_bytes())) for p in samples)
                est_mb = (total_final / len(samples)) * len(pngs) * (4 / 3) / (1<<20)

                if gen == self._est_generation:
                    self.after(0, lambda: self.var_est_size.set(f"~ {est_mb:.2f} MB"))
                    # Сохраняем ссылку на картинку, чтобы Python её не удалил (garbage collection)
                    self._test_photo_ref = tk_img 
                    self.after(0, lambda: self.lbl_quality_compare.config(image=tk_img, text=""))
                    
            except Exception as e:
                if gen == self._est_generation:
                    self.after(0, lambda: self.var_est_size.set(f"Ошибка: {e}"))
                    self.after(0, lambda: self.lbl_quality_compare.config(image="", text="❌ Ошибка сжатия"))

        threading.Thread(target=_task, daemon=True).start()

    def _log_msg(self, msg: str):
        self.ui_queue.put(("log", msg))

    def _run_conversion(self):
        # Если процесс уже идет — кнопка работает как "Стоп"
        if self.worker_active:
            self._cancel_conversion.set()
            self.btn_run.config(text="⏳ Остановка...", state="disabled")
            self._log_msg("\n🛑 Подана команда на остановку. Завершаю процессы...")
            return

        jp = self.var_json.get().strip()
        imd = self.var_images.get().strip()
        outp = self.var_output.get().strip()
        
        tasks = []
        is_queue_render = False

        # Определяем: соло-режим или очередь
        if jp and imd and outp:
            if not self._validate_lottie_json(Path(jp)):
                messagebox.showwarning("Ошибка", "Кажется, это не Lottie-анимация. Проверьте файл.")
                return
            tasks = [{"json_path": Path(jp), "img_dir": Path(imd), "out_path": Path(outp)}]
            is_queue_render = False
        elif len(self.render_queue) > 0:
            tasks = self.render_queue.copy()
            is_queue_render = True
        else:
            messagebox.showwarning("Внимание", "Выберите файл или добавьте файлы в очередь.")
            return

        fmt = "webp" if self.var_use_limit.get() else self.var_format.get()
        use_webp = fmt in ("webp", "lossless")
        lossless = (fmt == "lossless" and not self.var_use_limit.get())
        quality = self.var_quality.get()
        limit_mb = self.var_limit_mb.get() if self.var_use_limit.get() else 0.0
        
        if use_webp and not check_pillow():
            messagebox.showerror("Ошибка", "Для WebP нужен Pillow.")
            return

        self._est_cancel_event.set()
        self._cancel_conversion.clear() # Сбрасываем флаг перед началом
        self.btn_run.config(state="normal", text="🛑 Остановить!", style="Stop.TButton")
        self.btn_queue.config(state="disabled")
        self.worker_active = True
        
        self.notebook.select(self.tab_log)
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")

        total_tasks = len(tasks)
        if total_tasks > 1:
            self._log_msg(f"=== Запуск пакетной обработки ({total_tasks} файлов) ===\n")

        def _worker_thread():
            try:
                # Остановка старого сервера перед новым запуском
                if self.var_preview.get():
                    self.server_manager.stop(log_fn=self._log_msg)
                
                results_paths = []
                for idx, task in enumerate(tasks):
                    # ПРОВЕРКА ОТМЕНЫ ПЕРЕД КАЖДЫМ ФАЙЛОМ
                    if self._cancel_conversion.is_set():
                        break
                        
                    if total_tasks > 1:
                        self._log_msg(f"⏳ [{idx+1}/{total_tasks}] Обработка: {task['json_path'].name}")
                    
                    def _local_progress(cur, tot, eta, est, _idx=idx):
                        base_pct = (_idx / total_tasks) * 100
                        local_pct = (cur / tot) * (100 / total_tasks) if tot else 0
                        display_est = est if total_tasks == 1 else 0
                        self.ui_queue.put(("progress", base_pct + local_pct, eta, display_est))
                    
                    res = LottieProcessor.embed_images(
                        input_json=task['json_path'], images_dir=task['img_dir'], output_json=task['out_path'],
                        img_format=fmt, quality=quality, size_limit_mb=limit_mb,
                        log_fn=self._log_msg, progress_fn=_local_progress,
                        cancel_check=lambda: self._cancel_conversion.is_set()
                    )
                    
                    # ПРАВИЛЬНАЯ ПРОВЕРКА: Если не нажали СТОП, добавляем файл в список для просмотра
                    if res.get("path") and not self._cancel_conversion.is_set(): 
                        results_paths.append(res["path"])
                    
                    if total_tasks > 1 and not self._cancel_conversion.is_set():
                        self._log_msg(f"✅ Готово: {task['out_path'].name}\n")

                # Если нажали отмену, просто выходим
                if self._cancel_conversion.is_set():
                    return 

                # ЗАПУСК СЕРВЕРА: Теперь results_paths не будет пустым!
                if self.var_preview.get() and results_paths:
                    self.server_manager.start(results_paths, log_fn=self._log_msg)
                
                if self.var_open_folder.get() and results_paths:
                    folders = {p.parent for p in results_paths}
                    for folder in folders:
                        if sys.platform == "win32": os.startfile(folder)
                        else: subprocess.Popen(["open", str(folder)] if sys.platform == "darwin" else ["xdg-open", str(folder)])

            except Exception as e:
                self.ui_queue.put(("error", str(e)))
            finally:
                self.ui_queue.put(("done", is_queue_render))

        threading.Thread(target=_worker_thread, daemon=True).start()

    def _check_for_updates(self):
        """Фоновая проверка обновлений через GitHub API"""
        # ВАЖНО: Замени 'ТВОЙ_ЛОГИН/ТВОЙ_РЕПОЗИТОРИЙ' на реальные данные
        github_repo = "Griboed256/Lottie-Embed-Images.git"

        def check():
            import urllib.request
            import json
            import webbrowser
            from tkinter import messagebox

            api_url = "https://api.github.com/repos/Griboed256/Lottie-Embed-Images/releases/latest"
            
            try:
                # Делаем запрос к GitHub
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                
                latest_version = data.get("tag_name", "")
                release_url = data.get("html_url", "")
                release_notes = data.get("body", "Нет описания обновления.")

                # Используем нашу глобальную APP_VERSION
                def _parse_ver(v: str):
                    # Извлекаем все числа из строки версии для корректного сравнения
                    import re
                    nums = re.findall(r'\d+', v)
                    return tuple(int(x) for x in nums) if nums else (0,)

                if latest_version and _parse_ver(latest_version) > _parse_ver(APP_VERSION):
                    def show_alert():
                        msg = f"Доступна новая версия: {latest_version}!\nТекущая версия: {APP_VERSION}\n\nЧто нового:\n{release_notes}\n\nХотите скачать обновление?"
                        if messagebox.askyesno("Обновление", msg):
                            webbrowser.open(release_url)
                    
                    self.after(0, show_alert)

            except Exception as e:
                # Если нет интернета или GitHub недоступен — просто молчим, чтобы не бесить пользователя
                self._log_msg(f"  ⚠️ Проверка обновлений недоступна.")

        # Запускаем в фоновом потоке, чтобы программа не зависла при старте
        threading.Thread(target=check, daemon=True).start()

    def _process_ui_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    self.txt_log.config(state="normal")
                    self.txt_log.insert("end", item[1] + "\n")
                    self.txt_log.see("end")
                    self.txt_log.config(state="disabled")
                elif kind == "progress":
                    _, pct, eta, est = item
                    self.var_progress.set(pct)
                    if pct >= 100:
                        self.var_eta.set(f"✅ Готово! | {est:.2f} MB" if est > 0 else "✅ Обработка завершена!")
                    else:
                        self.var_eta.set(f"{int(pct)}% — ~{ImageUtils.format_eta(eta)}" + (f" | ≈ {est:.2f} MB" if est > 0 else ""))
                elif kind == "error":
                    self._log_msg(f"\n[ОШИБКА] {item[1]}")
                    self.var_eta.set("❌ Ошибка")
                elif kind == "done":
                    is_queue_render = item[1] if len(item) > 1 else False
                    
                    if is_queue_render and not self._cancel_conversion.is_set():
                        for task in self.render_queue:
                            if "widget" in task: task["widget"].destroy()
                        self.render_queue.clear()
                        self._update_queue_ui()
                    
                    if self._cancel_conversion.is_set():
                        self.var_eta.set("🛑 Обработка отменена")
                        
                    # Возвращаем стиль кнопки на стандартный синий
                    self.btn_run.config(state="normal", style="Run.TButton")
                    self.btn_queue.config(state="normal")
                    self._update_run_button_text()
                    self.worker_active = False
        except queue.Empty:
            pass

        if self.worker_active or not self.ui_queue.empty():
            self.after(40, self._process_ui_queue)
        else:
            self.after(200, self._process_ui_queue)

    def _show_startup_log(self):
        self._log_msg("Готов к работе. Заполни данные слева и нажми ▶ Запустить.\nДля пакетной обработки используй кнопку «➕ В очередь».\n")

    def _on_close(self):
        self.server_manager.stop()
        self.destroy()

# ===========================================================================
# Точка входа (GUI + CLI + Тихий Drag & Drop на EXE)
# ===========================================================================
if __name__ == "__main__":
    multiprocessing.freeze_support()
    
    import argparse
    # Настраиваем аргументы командной строки
    parser = argparse.ArgumentParser(description="Lottie Embed Images - Упаковщик PNG в Lottie JSON")
    parser.add_argument("--cli", action="store_true", help="Принудительный консольный режим (с логами)")
    
    # ИЗМЕНЕНИЕ 1: Теперь принимаем бесконечное количество путей (nargs="*")
    parser.add_argument("inputs", nargs="*", help="Пути к JSON файлам или папкам (Drag & Drop на EXE)")
    
    parser.add_argument("--images", help="Путь к папке images (только для одного файла)")
    parser.add_argument("--output", help="Путь к итоговому JSON (только для одного файла)")
    parser.add_argument("--format", choices=["webp", "avif", "png8", "lossless"], default="webp", help="Формат сжатия")
    parser.add_argument("--quality", type=int, default=85, help="Качество WebP (0-100)")
    parser.add_argument("--limit", type=float, default=0.0, help="Лимит размера в МБ")
    
    args, unknown = parser.parse_known_args()

    # --- CLI РЕЖИМ ИЛИ ТИХИЙ DRAG & DROP НА EXE ---
    if args.cli or args.inputs:
        valid_paths = [Path(p).resolve() for p in args.inputs if Path(p).exists()]
        
        if not valid_paths:
            sys.exit(1)

        # ИЗМЕНЕНИЕ 2: Умный сбор файлов (как в основном GUI)
        jsons_to_process = []
        for p in valid_paths:
            if p.is_file() and p.suffix.lower() == ".json" and not p.name.endswith("_result.json"):
                jsons_to_process.append(p)
            elif p.is_dir():
                for j in p.rglob("*.json"):
                    if not j.name.endswith("_result.json"):
                        jsons_to_process.append(j)

        if not jsons_to_process:
            sys.exit(0) # Ничего не нашли - тихо закрываемся

        use_webp = args.format in ("webp", "lossless")
        lossless = (args.format == "lossless")
        processed_count = 0
        
        # Определяем логгер (в тихом режиме он ничего не делает, в CLI - печатает)
        log_function = print if args.cli else lambda msg: None

        if args.cli:
            print("=== Lottie Embed Images (CLI / Batch Mode) ===")
            print(f"Найдено файлов для обработки: {len(jsons_to_process)}")

        for jp in jsons_to_process:
            # Быстрая проверка, что это Lottie
            try:
                with open(jp, "r", encoding="utf-8") as f:
                    if not json.load(f).get("layers"): continue
            except Exception: continue

            # Если файлов много, кастомные output/images из аргументов игнорируются
            img_dir = Path(args.images).resolve() if args.images and len(jsons_to_process) == 1 else jp.parent / "images"
            out_json = Path(args.output).resolve() if args.output and len(jsons_to_process) == 1 else jp.parent / f"{jp.stem}_result.json"

            try:
                LottieProcessor.embed_images(
                    input_json=jp, images_dir=img_dir, output_json=out_json,
                    img_format=args.format, quality=args.quality, size_limit_mb=args.limit,
                    log_fn=log_function
                )
                processed_count += 1
            except Exception as e:
                if args.cli: print(f"❌ Ошибка с {jp.name}: {e}")

        # ИЗМЕНЕНИЕ 3: Сообщение о готовности для тихого режима
        # Если запустили перетаскиванием на ярлык (нет флага --cli), покажем всплывающее окно
        if not args.cli and processed_count > 0:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw() # Прячем главное окно
                root.attributes('-topmost', True) # Выводим поверх всех окон
                messagebox.showinfo("Lottie Embed Images", f"✅ Тихая обработка завершена!\n\nУспешно упаковано файлов: {processed_count}")
                root.destroy()
            except Exception:
                pass

        sys.exit(0)

    # --- GUI РЕЖИМ (ОБЫЧНЫЙ ЗАПУСК КЛИКОМ ПО EXE) ---
    enable_dpi_awareness()

    if not _RUNNING_AS_BUNDLE:
        splash = SplashScreen()
        splash.mainloop()
        if not splash.go_ahead:
            sys.exit(0)

    app = LottieEmbedApp()
    app.mainloop()
