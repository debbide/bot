"""
bot-hosting.net 自动续期 — 一体化脚本
- 自己调用 convert_proxy.py 生成 config.json
- 自己起 sing-box 后台进程，暴露 http://127.0.0.1:8080
- 自己发 Telegram 通知（开始 / 成功+截图 / 失败+debug）
- SeleniumBase UC 过 Cloudflare Turnstile
- 通过 localStorage.token 登录

环境变量：
  TOKEN          bot-hosting.net localStorage token（必需）
  PROXY_STR      代理链接原文，如 vless://... （可选，空则直连）
  TG_BOT_TOKEN   Telegram bot token（可选）
  TG_CHAT_ID     Telegram chat id（可选）
"""

import os
import sys
import time
import atexit
import signal
import socket
import shutil
import subprocess
import traceback
from pathlib import Path

import requests
from seleniumbase import SB

HERE = Path(__file__).resolve().parent
HOME_URL = "https://bot-hosting.net/"
BILLINGS_URL = "https://bot-hosting.net/a/billings"
RENEW_TEXT = "Renew"
LOCAL_PROXY = "http://127.0.0.1:8080"
SING_BOX_PORT = 8080
COOLDOWN_BETWEEN_CLICKS = 3

# 全局 sing-box 进程句柄，方便 atexit 清理
_singbox_proc: subprocess.Popen | None = None


# ============ Telegram ============

def _tg_enabled() -> bool:
    return bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"))


def tg_text(text: str) -> None:
    if not _tg_enabled():
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/sendMessage",
            data={
                "chat_id": os.environ["TG_CHAT_ID"],
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
        print(f"[TG] sendMessage {r.status_code}")
    except Exception as e:
        print(f"[TG] sendMessage failed: {e}")


def tg_file(path: str | Path, caption: str = "", kind: str = "document") -> None:
    if not _tg_enabled():
        return
    p = Path(path)
    if not p.is_file():
        return
    method = "sendPhoto" if kind == "photo" else "sendDocument"
    field = "photo" if kind == "photo" else "document"
    try:
        with open(p, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}",
                data={"chat_id": os.environ["TG_CHAT_ID"], "caption": caption[:1000]},
                files={field: f},
                timeout=60,
            )
        print(f"[TG] {method} {p.name} {r.status_code}")
    except Exception as e:
        print(f"[TG] {method} failed: {e}")


# ============ sing-box ============

def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_singbox(proxy_str: str) -> None:
    """调用 convert_proxy.py 生成 config.json，然后后台起 sing-box。"""
    global _singbox_proc

    if not shutil.which("sing-box"):
        raise RuntimeError("sing-box 未安装或不在 PATH 里")

    convert = HERE / "convert_proxy.py"
    if not convert.is_file():
        raise RuntimeError(f"找不到 {convert}")

    # 生成 config.json（convert_proxy.py 从环境变量 PROXY_STR 读，写到当前目录）
    print("→ 调用 convert_proxy.py 生成 config.json ...")
    env = dict(os.environ)
    env["PROXY_STR"] = proxy_str
    r = subprocess.run(
        [sys.executable, str(convert)],
        cwd=HERE,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(r.stdout, end="")
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise RuntimeError("convert_proxy.py 生成 config.json 失败")

    config_path = HERE / "config.json"
    if not config_path.is_file():
        raise RuntimeError("config.json 未生成")

    print("→ 启动 sing-box ...")
    log_path = HERE / "sing-box.log"
    log_f = open(log_path, "wb")
    _singbox_proc = subprocess.Popen(
        ["sing-box", "run", "-c", str(config_path)],
        cwd=HERE,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    atexit.register(stop_singbox)

    # 等端口起来
    for _ in range(20):
        if _singbox_proc.poll() is not None:
            raise RuntimeError(f"sing-box 提前退出 rc={_singbox_proc.returncode}，看 sing-box.log")
        if _port_open("127.0.0.1", SING_BOX_PORT):
            print(f"  ✓ sing-box 监听 :{SING_BOX_PORT}")
            break
        time.sleep(1)
    else:
        raise RuntimeError("sing-box 20 秒内没起来")


def stop_singbox() -> None:
    global _singbox_proc
    if _singbox_proc and _singbox_proc.poll() is None:
        try:
            _singbox_proc.send_signal(signal.SIGTERM)
            _singbox_proc.wait(timeout=5)
        except Exception:
            try:
                _singbox_proc.kill()
            except Exception:
                pass
    _singbox_proc = None


def verify_proxy_ip() -> None:
    """打印直连 IP 和走代理 IP，方便排查"""
    try:
        r = requests.get("https://api.ipify.org", timeout=10)
        print(f"  直连 IP: {r.text}")
    except Exception as e:
        print(f"  直连 IP 失败: {e}")
    try:
        r = requests.get(
            "https://api.ipify.org",
            proxies={"http": LOCAL_PROXY, "https": LOCAL_PROXY},
            timeout=15,
        )
        print(f"  代理 IP: {r.text}")
    except Exception as e:
        print(f"  代理 IP 失败: {e}")


# ============ 续期主流程 ============

def do_renew(use_proxy: bool) -> tuple[bool, int]:
    """返回 (成功, 点击的 Renew 数)"""
    sb_kwargs = dict(
        uc=True,
        test=False,
        headed=True,
        xvfb=True,
        locale="en",
    )
    if use_proxy:
        # SeleniumBase 接受 host:port 或 user:pass@host:port，不带 scheme
        sb_kwargs["proxy"] = LOCAL_PROXY.replace("http://", "")

    with SB(**sb_kwargs) as sb:
        # 1) 首页过盾
        print("\n→ 打开首页，尝试过 Turnstile ...")
        sb.uc_open_with_reconnect(HOME_URL, 4)
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"  ℹ️  uc_gui_click_captcha: {e}")
        sb.sleep(3)

        # 2) 注入 localStorage.token
        print("→ 注入 localStorage.token ...")
        token = os.environ["TOKEN"]
        sb.execute_script("localStorage.setItem('token', arguments[0])", token)
        got = sb.execute_script("return localStorage.getItem('token')")
        if got != token:
            raise RuntimeError("localStorage 注入失败")
        print("  ✓ token 已写入")

        # 3) 跳 billings
        print(f"\n→ 跳转 {BILLINGS_URL}")
        sb.uc_open_with_reconnect(BILLINGS_URL, 4)
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"  ℹ️  uc_gui_click_captcha: {e}")
        sb.sleep(5)
        print(f"  页面标题: {sb.get_title()}")
        print(f"  当前 URL: {sb.get_current_url()}")

        # 4) 找 Renew 按钮
        xpath = (
            f"//button[normalize-space()='{RENEW_TEXT}']"
            f" | //a[normalize-space()='{RENEW_TEXT}']"
            f" | //button[contains(normalize-space(), '{RENEW_TEXT}')]"
        )
        print(f"\n→ 查找 '{RENEW_TEXT}' 按钮 ...")
        try:
            sb.wait_for_element(xpath, by="xpath", timeout=20)
        except Exception:
            print(f"  ✗ 20 秒内没找到 '{RENEW_TEXT}' 按钮，dump 页面")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            return False, 0

        buttons = sb.find_elements(xpath, by="xpath")
        print(f"  ✓ 找到 {len(buttons)} 个按钮")

        # 5) 逐个点
        clicked = 0
        for i in range(len(buttons)):
            fresh = sb.find_elements(xpath, by="xpath")
            if i >= len(fresh):
                break
            btn = fresh[i]
            try:
                text = (btn.text or "").strip()
                enabled = btn.is_enabled()
                print(f"\n  [{i+1}/{len(buttons)}] '{text}' enabled={enabled}")
                if not enabled:
                    print("    ⚠️  按钮 disabled，跳过")
                    continue
                btn.click()
                clicked += 1
                sb.sleep(COOLDOWN_BETWEEN_CLICKS)

                # 处理 SweetAlert 确认
                try:
                    confirm = sb.find_element("button.swal-button--confirm", timeout=3)
                    if confirm and confirm.is_displayed():
                        confirm.click()
                        print("    ✓ 点掉确认弹窗")
                        sb.sleep(2)
                except Exception:
                    pass

                # 处理可能出现的二次盾
                try:
                    sb.uc_gui_click_captcha()
                except Exception:
                    pass
            except Exception as e:
                print(f"    ✗ 点击失败: {type(e).__name__}: {e}")

        sb.save_screenshot(str(HERE / "after_renew.png"))
        print(f"\n✅ 共点击 {clicked} 个 Renew 按钮")
        return clicked > 0, clicked


# ============ 入口 ============

def main() -> int:
    token = os.environ.get("TOKEN", "")
    if not token:
        print("✗ 缺少 TOKEN")
        tg_text("❌ <b>bot-hosting renew</b>\n缺少 TOKEN 环境变量")
        return 2

    proxy_str = os.environ.get("PROXY_STR", "").strip()
    use_proxy = bool(proxy_str)

    tg_text(
        f"🚀 <b>bot-hosting renew</b>\n开始运行\n"
        f"proxy: {'on' if use_proxy else 'off'}"
    )

    try:
        if use_proxy:
            start_singbox(proxy_str)
            print("\n→ 验证出口 IP ...")
            verify_proxy_ip()
        else:
            print("ℹ️  未配置 PROXY_STR，直连运行（GitHub Actions IP 可能被 CF 拉黑）")

        success, clicked = do_renew(use_proxy)

    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        tg_text(f"❌ <b>bot-hosting renew</b>\n<pre>{str(e)[:1000]}</pre>")
        for name, kind in [
            ("page_debug.png", "photo"),
            ("page_debug.html", "document"),
            ("sing-box.log", "document"),
        ]:
            tg_file(HERE / name, name, kind)
        return 1
    finally:
        stop_singbox()

    if success:
        tg_text(f"✅ <b>bot-hosting renew</b>\n成功，点击了 {clicked} 个按钮")
        tg_file(HERE / "after_renew.png", f"after_renew ({clicked} clicks)", "photo")
        return 0
    else:
        tg_text("❌ <b>bot-hosting renew</b>\n没找到可点的 Renew 按钮")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        tg_file(HERE / "page_debug.html", "page_debug.html", "document")
        tg_file(HERE / "sing-box.log", "sing-box.log", "document")
        return 1


if __name__ == "__main__":
    sys.exit(main())
